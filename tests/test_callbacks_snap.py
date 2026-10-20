from __future__ import annotations

import json

import httpx
import pytest

from app.config import get_settings
from app.services.job_runner import run_jobs
from tests.helpers import (
    FaspayMock,
    create,
    minified,
    payment_body,
    q,
    q1,
    reference_notification,
    run,
    sign_snap_callback,
    snap_status_reply,
    va_notification,
)
from tests.keys import MERCHANT_KEY

pytestmark = pytest.mark.integration

VA_PATH = "/v1.0/transfer-va/payment"
QRIS_PATH = "/v1.0/qr/qr-mpm-notify"
DEBIT_PATH = "/v1.0/debit/notify"
VA_STATUS = "/v1.0/transfer-va/status"


async def snap_post(
    api: httpx.AsyncClient,
    path: str,
    payload: dict,
    *,
    sign: bool = True,
    key=None,
    timestamp: str | None = None,
    raw: bytes | None = None,
) -> httpx.Response:
    body = raw if raw is not None else minified(payload)
    headers = {"Content-Type": "application/json"}
    if sign:
        kwargs = {"key": key} if key is not None else {}
        headers = sign_snap_callback(path, body, timestamp=timestamp, **kwargs)
    return await api.post(path, content=body, headers=headers)


async def make_va(api, seed, auth_headers, **overrides) -> dict:
    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers, payment_body(**overrides))
    assert resp.status_code == 201, resp.text
    payment = resp.json()
    row = await q1(
        "SELECT provider_partner_reference AS ref FROM payment_transactions WHERE id = :id",
        id=payment["payment_id"],
    )
    return {**payment, "partner_ref": row["ref"]}


async def payment_status(payment_id: str) -> str:
    return (await q1("SELECT status FROM payment_transactions WHERE id=:id", id=payment_id))[
        "status"
    ]


async def test_va_success_confirmed_by_inquiry(api, seed, auth_headers, faspay):
    payment = await make_va(api, seed, auth_headers)
    faspay.set(VA_STATUS, snap_status_reply("00", va=True, amount=150000))

    resp = await snap_post(api, VA_PATH, va_notification(payment["partner_ref"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["responseCode"] == "2002500" and body["responseMessage"] == "Successful"
    assert "X-TIMESTAMP" in resp.headers or "x-timestamp" in resp.headers
    echo = body["virtualAccountData"]
    assert echo["customerNo"] == "00000001" and echo["paidAmount"] == {
        "value": "150000.00",
        "currency": "IDR",
    }

    row = await q1("SELECT status, paid_at, provider_status FROM payment_transactions")
    assert row["status"] == "SUCCEEDED" and row["paid_at"] and row["provider_status"] == "00"
    (inquiry,) = faspay.calls_to(VA_STATUS)
    assert inquiry.signature_ok is True
    assert inquiry.body["additionalInfo"]["trxId"] == payment["partner_ref"]
    assert inquiry.body["partnerServiceId"] == "   88888"  # from the create response

    event = await q1("SELECT * FROM webhook_events")
    assert event["processing_status"] == "PROCESSED"
    assert event["verification_method"] == "SNAP_SIGNATURE_AND_INQUIRY"
    assert event["signature_valid"] is True
    assert event["payment_id"] is not None
    assert "Contoh" not in json.dumps(event["payload_redacted"])  # PII redacted
    pe = await q1("SELECT * FROM payment_events WHERE event_type='payment.succeeded'")
    assert pe["source"] == "CALLBACK" and pe["webhook_event_id"] == event["id"]
    delivery = await q1("SELECT * FROM webhook_deliveries")
    assert delivery["delivery_status"] == "PENDING"
    assert delivery["destination_url"] == "https://consumer.test/cf/hook"
    job = await q1("SELECT job_type, ref_id FROM gateway_jobs")
    assert job == {"job_type": "DELIVER_WEBHOOK", "ref_id": delivery["id"]}
    attempt_count = await q1("SELECT count(*) AS n FROM payment_attempts WHERE operation='INQUIRY'")
    assert attempt_count["n"] == 0  # callback inquiries are not payment attempts


async def test_signature_only_mode_skips_the_inquiry(api, seed, auth_headers, faspay):
    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    payment = await make_va(api, seed, auth_headers)
    resp = await snap_post(api, VA_PATH, va_notification(payment["partner_ref"]))
    assert resp.status_code == 200
    assert faspay.calls_to(VA_STATUS) == []
    assert await payment_status(payment["payment_id"]) == "SUCCEEDED"
    event = await q1("SELECT verification_method FROM webhook_events")
    assert event["verification_method"] == "SNAP_SIGNATURE"


async def test_duplicate_callback_is_acknowledged_without_new_effects(
    api, seed, auth_headers, faspay
):
    payment = await make_va(api, seed, auth_headers)
    faspay.set(VA_STATUS, snap_status_reply("00", va=True, amount=150000))
    notification = va_notification(payment["partner_ref"])
    first = await snap_post(api, VA_PATH, notification)
    second = await snap_post(api, VA_PATH, notification)
    assert first.status_code == second.status_code == 200
    assert second.json()["responseCode"] == "2002500"
    assert (await q1("SELECT count(*) AS n FROM webhook_events"))["n"] == 1
    assert (await q1("SELECT count(*) AS n FROM webhook_deliveries"))["n"] == 1
    assert (
        await q1("SELECT count(*) AS n FROM payment_events WHERE event_type='payment.succeeded'")
    )["n"] == 1
    assert len(faspay.calls_to(VA_STATUS)) == 1  # the duplicate did not trigger another inquiry

    get_settings().SNAP_DUPLICATE_ACK_MODE = "CONFLICT"
    conflict = await snap_post(api, VA_PATH, notification)
    assert conflict.status_code == 409 and conflict.json()["responseCode"] == "4092501"


async def test_amount_mismatch_is_not_applied(api, seed, auth_headers, faspay):
    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    payment = await make_va(api, seed, auth_headers)
    resp = await snap_post(api, VA_PATH, va_notification(payment["partner_ref"], amount=1000))
    assert resp.status_code == 404 and resp.json()["responseCode"] == "4042513"
    assert await payment_status(payment["payment_id"]) == "PENDING"
    assert (await q1("SELECT processing_status FROM webhook_events"))[
        "processing_status"
    ] == "FAILED"
    assert await q("SELECT 1 FROM gateway_jobs") == []


async def test_invalid_and_tampered_signatures_are_rejected(api, seed, auth_headers, faspay):
    from tests.keys import _pair

    payment = await make_va(api, seed, auth_headers)
    notification = va_notification(payment["partner_ref"])

    wrong_key = await snap_post(api, VA_PATH, notification, key=_pair()[0])
    assert wrong_key.status_code == 401 and wrong_key.json()["responseCode"] == "4012500"

    signed = sign_snap_callback(VA_PATH, minified(notification))
    tampered = {**notification, "paidAmount": {"value": "1.00", "currency": "IDR"}}
    resp = await api.post(VA_PATH, content=minified(tampered), headers=signed)
    assert resp.status_code == 401

    stale = await snap_post(api, VA_PATH, notification, timestamp="2020-01-01T00:00:00+07:00")
    assert stale.status_code == 401

    assert await payment_status(payment["payment_id"]) == "PENDING"
    rejected = await q("SELECT processing_status, signature_valid FROM webhook_events")
    assert len(rejected) == 3
    assert {r["processing_status"] for r in rejected} == {"REJECTED"}
    assert not any(r["signature_valid"] for r in rejected)
    assert faspay.calls_to(VA_STATUS) == []  # forged calls never reach Faspay


async def test_forged_payload_cannot_block_the_genuine_callback(api, seed, auth_headers, faspay):
    from tests.keys import _pair

    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    payment = await make_va(api, seed, auth_headers)
    notification = va_notification(payment["partner_ref"])
    forged = await snap_post(api, VA_PATH, notification, key=_pair()[0])
    assert forged.status_code == 401
    genuine = await snap_post(api, VA_PATH, notification)
    assert genuine.status_code == 200
    assert await payment_status(payment["payment_id"]) == "SUCCEEDED"


async def test_missing_signature_falls_back_to_inquiry_only(api, seed, auth_headers, faspay):
    payment = await make_va(api, seed, auth_headers)
    faspay.set(VA_STATUS, snap_status_reply("00", va=True, amount=150000))
    resp = await snap_post(api, VA_PATH, va_notification(payment["partner_ref"]), sign=False)
    assert resp.status_code == 200
    assert await payment_status(payment["payment_id"]) == "SUCCEEDED"
    event = await q1("SELECT verification_method, signature_valid FROM webhook_events")
    assert event == {"verification_method": "SNAP_INQUIRY_ONLY", "signature_valid": False}


async def test_unsigned_callback_is_only_a_hint(api, seed, auth_headers, faspay):
    """A forged 'paid' notification with no signature cannot move money without the inquiry."""
    payment = await make_va(api, seed, auth_headers)
    faspay.set(VA_STATUS, snap_status_reply("01", va=True))  # Faspay says: still paying
    resp = await snap_post(api, VA_PATH, va_notification(payment["partner_ref"]), sign=False)
    assert resp.status_code == 500  # not acknowledged: Faspay retries
    assert await payment_status(payment["payment_id"]) == "PENDING"
    follow_up = await q1("SELECT job_type, args FROM gateway_jobs")
    assert follow_up["job_type"] == "INQUIRE_PAYMENT" and follow_up["args"] == {
        "expect": "SUCCEEDED"
    }


async def test_missing_signature_is_rejected_in_signature_mode(api, seed, auth_headers, faspay):
    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    payment = await make_va(api, seed, auth_headers)
    resp = await snap_post(api, VA_PATH, va_notification(payment["partner_ref"]), sign=False)
    assert resp.status_code == 401
    assert await payment_status(payment["payment_id"]) == "PENDING"


async def test_inquiry_disagreement_and_failure_are_retryable(api, seed, auth_headers, faspay):
    payment = await make_va(api, seed, auth_headers)
    notification = va_notification(payment["partner_ref"])

    faspay.set(VA_STATUS, snap_status_reply("01", va=True))
    disagree = await snap_post(api, VA_PATH, notification)
    assert disagree.status_code == 500 and disagree.json()["responseCode"] == "5002501"

    faspay.set(VA_STATUS, {"responseCode": "5002600"}, status=500)
    broken = await snap_post(api, VA_PATH, notification)
    assert broken.status_code == 500

    assert await payment_status(payment["payment_id"]) == "PENDING"
    # The rejected rows carry random salts, so the genuine retry is not blocked.
    faspay.set(VA_STATUS, snap_status_reply("00", va=True, amount=150000))
    ok = await snap_post(api, VA_PATH, notification)
    assert ok.status_code == 200
    assert await payment_status(payment["payment_id"]) == "SUCCEEDED"


async def test_non_success_statuses(api, seed, auth_headers, faspay):
    # Signed non-success statuses are applied without an inquiry.
    expired = await make_va(api, seed, auth_headers)
    resp = await snap_post(api, VA_PATH, va_notification(expired["partner_ref"], status="07"))
    assert resp.status_code == 200
    assert await payment_status(expired["payment_id"]) == "EXPIRED"
    assert faspay.calls_to(VA_STATUS) == []
    job = await q1("SELECT job_type FROM gateway_jobs")
    assert job["job_type"] == "DELIVER_WEBHOOK"  # payment.expired is delivered

    cancelled = await make_va(api, seed, auth_headers)
    await snap_post(
        api, VA_PATH, va_notification(cancelled["partner_ref"], va_no="8888800000002", status="05")
    )
    assert await payment_status(cancelled["payment_id"]) == "CANCELLED"

    # Refund is recorded but never changes the status; an unmapped value is stored and alerted.
    refund = await make_va(api, seed, auth_headers)
    r = await snap_post(
        api, VA_PATH, va_notification(refund["partner_ref"], va_no="8888800000003", status="04")
    )
    assert r.status_code == 200
    assert await payment_status(refund["payment_id"]) == "PENDING"
    weird = await snap_post(
        api, VA_PATH, va_notification(refund["partner_ref"], va_no="8888800000003", status="42")
    )
    assert weird.status_code == 200
    assert await payment_status(refund["payment_id"]) == "PENDING"
    assert (
        await q1(
            "SELECT provider_status FROM payment_transactions WHERE id=:i", i=refund["payment_id"]
        )
    )["provider_status"] == "42"

    # A pending status moves nothing.
    pending = await make_va(api, seed, auth_headers)
    await snap_post(
        api, VA_PATH, va_notification(pending["partner_ref"], va_no="8888800000004", status="01")
    )
    assert await payment_status(pending["payment_id"]) == "PENDING"


async def test_status_map_override_from_configuration(api, seed, auth_headers, faspay):
    get_settings().SNAP_STATUS_MAP_OVERRIDES = {"07": "RECORD"}  # Faspay: 07 means "not found"
    payment = await make_va(api, seed, auth_headers)
    resp = await snap_post(api, VA_PATH, va_notification(payment["partner_ref"], status="07"))
    assert resp.status_code == 200
    assert await payment_status(payment["payment_id"]) == "PENDING"


async def test_unmatched_event_is_stored_and_answered_not_found(api, seed, auth_headers, faspay):
    await make_va(api, seed, auth_headers)
    resp = await snap_post(api, VA_PATH, va_notification("NOSUCHREF0000000", amount=150000))
    assert resp.status_code == 404 and resp.json()["responseCode"] == "4042512"
    event = await q1("SELECT processing_status, payment_id FROM webhook_events")
    assert event == {"processing_status": "UNMATCHED", "payment_id": None}
    job = await q1("SELECT job_type, status FROM gateway_jobs")
    assert job["job_type"] == "RETRY_CORRELATION"
    assert faspay.calls_to(VA_STATUS) == []  # no inquiry for references we do not know


async def test_unknown_merchant_and_malformed_bodies(api, seed, auth_headers, faspay):
    payment = await make_va(api, seed, auth_headers)
    other = await snap_post(
        api, VA_PATH, va_notification(payment["partner_ref"], merchant_id="00000")
    )
    assert other.status_code == 404

    bad_json = await snap_post(api, VA_PATH, {}, raw=b"{nope")
    assert bad_json.status_code == 400 and bad_json.json()["responseCode"] == "4002501"
    missing = va_notification(payment["partner_ref"])
    del missing["trxId"]
    resp = await snap_post(api, VA_PATH, missing)
    assert resp.status_code == 400 and resp.json()["responseCode"] == "4002502"
    unknown_shape = await snap_post(api, VA_PATH, {"hello": "world"})
    assert unknown_shape.status_code == 400
    bad_amount = va_notification(payment["partner_ref"])
    bad_amount["paidAmount"] = {"value": "150000.50", "currency": "IDR"}
    assert (await snap_post(api, VA_PATH, bad_amount)).json()["responseCode"] == "4002501"
    assert await payment_status(payment["payment_id"]) == "PENDING"


async def test_qris_reference_shape(api, seed, auth_headers, faspay):
    headers = await auth_headers(api, seed.cf)
    created = (
        await create(api, headers, payment_body(payment_method="QRIS", channel_code="715"))
    ).json()
    ref = (await q1("SELECT provider_partner_reference AS r FROM payment_transactions"))["r"]
    faspay.set("/v1.0/qr/qr-mpm-query", snap_status_reply("00", amount=150000))

    resp = await snap_post(
        api, QRIS_PATH, reference_notification(ref, provider_reference="QR-REF-1")
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "responseCode": "2005200",
        "responseMessage": "Request has been processed successfully",
    }
    assert await payment_status(created["payment_id"]) == "SUCCEEDED"
    query = faspay.calls_to("/v1.0/qr/qr-mpm-query")[0].body
    assert query["originalReferenceNo"] == "QR-REF-1" and query["serviceCode"] == "51"


async def test_qris_query_service_code_is_configurable(api, seed, auth_headers, faspay):
    get_settings().SNAP_QRIS_QUERY_SERVICE_CODE = "47"
    headers = await auth_headers(api, seed.cf)
    await create(api, headers, payment_body(payment_method="QRIS", channel_code="715"))
    ref = (await q1("SELECT provider_partner_reference AS r FROM payment_transactions"))["r"]
    faspay.set("/v1.0/qr/qr-mpm-query", snap_status_reply("00", amount=150000))
    await snap_post(api, QRIS_PATH, reference_notification(ref))
    assert faspay.calls_to("/v1.0/qr/qr-mpm-query")[0].body["serviceCode"] == "47"


async def test_ewallet_notification_on_the_va_path_is_accepted(api, seed, auth_headers, faspay):
    headers = await auth_headers(api, seed.cf)
    created = (
        await create(api, headers, payment_body(payment_method="EWALLET", channel_code="722"))
    ).json()
    ref = (await q1("SELECT provider_partner_reference AS r FROM payment_transactions"))["r"]
    faspay.set("/v1.0/debit/status", snap_status_reply("00", amount=150000))
    notification = reference_notification(ref, provider_reference="EW-REF-1", channel="713")

    # Documented route and the route the engineering team reports both reach the same handler.
    on_va_path = await snap_post(api, VA_PATH, notification)
    assert on_va_path.status_code == 200
    assert on_va_path.json()["responseCode"] == "2005600"  # service 56 after the lookup
    assert await payment_status(created["payment_id"]) == "SUCCEEDED"
    again = await snap_post(api, DEBIT_PATH, notification)
    assert again.status_code == 200  # duplicate on the other route is still acknowledged


async def test_incompatible_shape_is_unmatched_not_applied(api, seed, auth_headers, faspay):
    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    headers = await auth_headers(api, seed.cf)
    created = (
        await create(api, headers, payment_body(payment_method="QRIS", channel_code="715"))
    ).json()
    ref = (await q1("SELECT provider_partner_reference AS r FROM payment_transactions"))["r"]
    # A VA-shaped notification that names a QRIS payment's reference.
    resp = await snap_post(api, VA_PATH, va_notification(ref))
    assert resp.status_code == 404
    assert await payment_status(created["payment_id"]) == "PENDING"
    assert (await q1("SELECT processing_status FROM webhook_events"))[
        "processing_status"
    ] == "UNMATCHED"


async def test_provider_reference_mismatch_is_unmatched(api, seed, auth_headers, faspay):
    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    payment = await make_va(api, seed, auth_headers)
    resp = await snap_post(
        api, VA_PATH, va_notification(payment["partner_ref"], va_no="9999900000009")
    )
    assert resp.status_code == 404
    assert await payment_status(payment["payment_id"]) == "PENDING"


async def test_callback_before_create_response_is_persisted(api, seed, auth_headers, faspay):
    payment = await make_va(api, seed, auth_headers)
    pid = payment["payment_id"]
    # Simulate the race: the provider call is still in flight (payment CREATED, no reference).
    await run(
        "UPDATE payment_transactions SET status='CREATED', provider_reference=NULL WHERE id=:i",
        i=pid,
    )
    faspay.set(VA_STATUS, snap_status_reply("00", va=True, amount=150000))
    notification = va_notification(payment["partner_ref"])

    early = await snap_post(api, VA_PATH, notification)
    assert early.status_code == 404  # Faspay may retry
    assert await payment_status(pid) == "CREATED"
    assert (await q1("SELECT processing_status FROM webhook_events"))[
        "processing_status"
    ] == "UNMATCHED"

    # The create response lands; the correlation retry now applies the held event.
    await run("UPDATE payment_transactions SET status='PENDING' WHERE id=:i", i=pid)
    await run("UPDATE gateway_jobs SET run_after = now()")
    summary = await run_jobs(job_types=["RETRY_CORRELATION"])
    assert summary.done == 1
    assert await payment_status(pid) == "SUCCEEDED"
    event = await q1("SELECT processing_status, payment_id FROM webhook_events")
    assert event["processing_status"] == "PROCESSED" and str(event["payment_id"]) == pid
    assert (await q1("SELECT count(*) AS n FROM webhook_deliveries"))["n"] == 1


async def test_faspay_retry_of_an_unmatched_event_is_recorrelated(api, seed, auth_headers, faspay):
    payment = await make_va(api, seed, auth_headers)
    pid = payment["payment_id"]
    await run("UPDATE payment_transactions SET status='CREATED' WHERE id=:i", i=pid)
    faspay.set(VA_STATUS, snap_status_reply("00", va=True, amount=150000))
    notification = va_notification(payment["partner_ref"])
    assert (await snap_post(api, VA_PATH, notification)).status_code == 404

    await run("UPDATE payment_transactions SET status='PENDING' WHERE id=:i", i=pid)
    retry = await snap_post(api, VA_PATH, notification)  # identical retry from Faspay
    assert retry.status_code == 200  # answered with the new result, not a blind success
    assert await payment_status(pid) == "SUCCEEDED"
    assert (await q1("SELECT count(*) AS n FROM webhook_events"))["n"] == 1


async def test_lost_create_response_is_recovered_by_the_callback(api, seed, auth_headers, faspay):
    faspay.set("/v1.0/transfer-va/create-va", exc=httpx.ReadTimeout("lost"))
    headers = await auth_headers(api, seed.cf)
    created = await create(api, headers, payment_body(merchant_reference="INV-LOST"), key="lost-1")
    assert created.status_code == 202
    pid = created.json()["payment_id"]
    ref = (await q1("SELECT provider_partner_reference AS r FROM payment_transactions"))["r"]
    assert (await q1("SELECT provider_reference FROM payment_transactions"))[
        "provider_reference"
    ] is None

    faspay.set(VA_STATUS, snap_status_reply("00", va=True, amount=150000))
    resp = await snap_post(api, VA_PATH, va_notification(ref, va_no="7777700000007"))
    assert resp.status_code == 200
    row = await q1("SELECT status, provider_reference FROM payment_transactions")
    assert row == {"status": "SUCCEEDED", "provider_reference": "7777700000007"}
    idem = await q1("SELECT operation_status FROM api_idempotency_keys")
    assert idem["operation_status"] == "COMPLETED"  # the unresolved key is now resolved
    replay = await create(api, headers, payment_body(merchant_reference="INV-LOST"), key="lost-1")
    assert replay.status_code == 200 and replay.json()["status"] == "SUCCEEDED"
    assert pid == replay.json()["payment_id"]


async def test_success_after_expiry_is_flagged_not_applied(api, seed, auth_headers, faspay):
    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    payment = await make_va(api, seed, auth_headers)
    await run("UPDATE payment_transactions SET status='EXPIRED'")
    resp = await snap_post(api, VA_PATH, va_notification(payment["partner_ref"]))
    assert resp.status_code == 404 and resp.json()["responseCode"] == "4042519"
    assert await payment_status(payment["payment_id"]) == "EXPIRED"
    assert (await q1("SELECT processing_status FROM webhook_events"))[
        "processing_status"
    ] == "FAILED"
    job = await q1("SELECT job_type FROM gateway_jobs")
    assert job["job_type"] == "RECONCILE_PAYMENT"


async def test_signature_over_the_reminified_body(api, seed, auth_headers, faspay):
    """Faspay may sign its own minified form while sending whitespace; both are accepted."""
    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    payment = await make_va(api, seed, auth_headers)
    notification = va_notification(payment["partner_ref"])
    headers = sign_snap_callback(VA_PATH, minified(notification))
    pretty = json.dumps(notification, indent=2).encode()
    resp = await api.post(VA_PATH, content=pretty, headers=headers)
    assert resp.status_code == 200


async def test_signature_header_and_path_mode_are_configurable(api, seed, auth_headers, faspay):
    settings = get_settings()
    settings.CALLBACK_VERIFY_MODE = "SIGNATURE"
    settings.SNAP_CALLBACK_PATH_MODES = "full_url"
    payment = await make_va(api, seed, auth_headers)
    body = minified(va_notification(payment["partner_ref"]))
    headers = sign_snap_callback("http://test" + VA_PATH, body)  # signed over the full URL
    headers["X-SIGNATURE"] = headers.pop("X-SIGNATURE_FROM_FASPAY")  # and the fallback header
    resp = await api.post(VA_PATH, content=body, headers=headers)
    assert resp.status_code == 200


async def test_merchant_key_is_never_confused_with_the_faspay_key(faspay: FaspayMock):
    assert MERCHANT_KEY is not None  # outbound requests are verified with the merchant key only
