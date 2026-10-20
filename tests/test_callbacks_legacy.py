from __future__ import annotations

import hashlib

import httpx
import pytest

from app.config import get_settings
from tests.helpers import (
    create,
    legacy_inquiry_reply,
    legacy_notification,
    payment_body,
    q,
    q1,
)

pytestmark = pytest.mark.integration

PATH = "/v1/webhooks/faspay/legacy/notification"
INQUIRY = "/cvr/100004/10"


async def make_retail(api, seed, auth_headers) -> dict:
    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers, payment_body(payment_method="RETAIL", channel_code="707"))
    assert resp.status_code == 201, resp.text
    row = await q1(
        "SELECT provider_partner_reference AS ref, provider_reference AS trx "
        "FROM payment_transactions WHERE id = :id",
        id=resp.json()["payment_id"],
    )
    return {**resp.json(), "bill_no": row["ref"], "trx_id": row["trx"]}


async def post(api: httpx.AsyncClient, payload: dict) -> httpx.Response:
    return await api.post(PATH, json=payload)


async def status_of(payment_id: str) -> str:
    return (await q1("SELECT status FROM payment_transactions WHERE id=:i", i=payment_id))["status"]


async def test_success_is_confirmed_by_the_inquiry(api, seed, auth_headers, faspay):
    payment = await make_retail(api, seed, auth_headers)
    faspay.set(INQUIRY, legacy_inquiry_reply("2"))
    resp = await post(api, legacy_notification(payment["bill_no"], trx_id=payment["trx_id"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["response"] == "Payment Notification"
    assert body["bill_no"] == payment["bill_no"] and body["trx_id"] == payment["trx_id"]
    assert body["merchant_id"] == "12345" and body["merchant"] == "Karina"
    assert body["response_code"] == "00" and body["response_date"]
    assert await status_of(payment["payment_id"]) == "SUCCEEDED"

    (call,) = faspay.calls_to(INQUIRY)
    expected = hashlib.sha1(  # noqa: S324
        hashlib.md5(f"bot123pass456{payment['bill_no']}".encode()).hexdigest().encode()  # noqa: S324
    ).hexdigest()
    assert call.body["signature"] == expected  # independently computed sha1(md5(...))
    assert call.body["trx_id"] == payment["trx_id"] and call.body["bill_no"] == payment["bill_no"]
    event = await q1("SELECT * FROM webhook_events")
    assert event["verification_method"] == "LEGACY_INQUIRY"
    assert event["processing_status"] == "PROCESSED"
    assert (await q1("SELECT job_type FROM gateway_jobs"))["job_type"] == "DELIVER_WEBHOOK"


async def test_notification_alone_never_marks_a_payment_succeeded(api, seed, auth_headers, faspay):
    payment = await make_retail(api, seed, auth_headers)
    faspay.set(INQUIRY, legacy_inquiry_reply("1"))  # Faspay: still in process
    resp = await post(api, legacy_notification(payment["bill_no"], trx_id=payment["trx_id"]))
    assert resp.status_code == 200
    assert resp.json()["response_code"] != "00"  # not acknowledged: Faspay retries
    assert await status_of(payment["payment_id"]) == "PENDING"
    job = await q1("SELECT job_type, args FROM gateway_jobs")
    assert job["job_type"] == "INQUIRE_PAYMENT" and job["args"] == {"expect": "SUCCEEDED"}

    # If the inquiry itself fails the reply is also non-success.
    faspay.set(INQUIRY, {"error": "x"}, status=500)
    broken = await post(api, legacy_notification(payment["bill_no"], trx_id=payment["trx_id"]))
    assert broken.json()["response_code"] != "00"
    assert await status_of(payment["payment_id"]) == "PENDING"


async def test_amounts_use_the_x100_format(api, seed, auth_headers, faspay):
    payment = await make_retail(api, seed, auth_headers)
    faspay.set(INQUIRY, legacy_inquiry_reply("2"))
    bad = await post(
        api, legacy_notification(payment["bill_no"], trx_id=payment["trx_id"], total="1000")
    )
    assert bad.json()["response_code"] != "00"  # 1000 means Rp 10, not Rp 150000
    assert await status_of(payment["payment_id"]) == "PENDING"
    assert (await q1("SELECT processing_status FROM webhook_events"))[
        "processing_status"
    ] == "FAILED"


async def test_missing_amount_is_not_applied_unless_configured(api, seed, auth_headers, faspay):
    payment = await make_retail(api, seed, auth_headers)
    faspay.set(INQUIRY, legacy_inquiry_reply("2"))
    notification = legacy_notification(payment["bill_no"], trx_id=payment["trx_id"], total=None)

    held = await post(api, notification)
    assert held.status_code == 200
    assert await status_of(payment["payment_id"]) == "PENDING"
    assert (await q1("SELECT job_type FROM gateway_jobs"))["job_type"] == "INQUIRE_PAYMENT"

    get_settings().LEGACY_REQUIRE_AMOUNT_FOR_SUCCESS = False
    notification["payment_status_desc"] = "again"  # a retry; same fingerprint, re-correlated
    applied = await post(api, notification)
    assert applied.status_code == 200
    assert await status_of(payment["payment_id"]) == "SUCCEEDED"


async def test_unmatched_bill_is_not_inquired(api, seed, auth_headers, faspay):
    await make_retail(api, seed, auth_headers)
    resp = await post(api, legacy_notification("UNKNOWNBILL00000"))
    assert resp.status_code == 200
    assert resp.json()["response_code"] == "99"
    assert faspay.calls_to(INQUIRY) == []  # forged references cannot make us call Faspay
    event = await q1("SELECT processing_status, payment_id FROM webhook_events")
    assert event == {"processing_status": "UNMATCHED", "payment_id": None}
    assert (await q1("SELECT job_type FROM gateway_jobs"))["job_type"] == "RETRY_CORRELATION"


async def test_duplicate_notification_is_acknowledged_once(api, seed, auth_headers, faspay):
    payment = await make_retail(api, seed, auth_headers)
    faspay.set(INQUIRY, legacy_inquiry_reply("2"))
    notification = legacy_notification(payment["bill_no"], trx_id=payment["trx_id"])
    await post(api, notification)
    again = await post(api, notification)
    assert again.status_code == 200 and again.json()["response_code"] == "00"
    assert len(faspay.calls_to(INQUIRY)) == 1
    assert (await q1("SELECT count(*) AS n FROM webhook_events"))["n"] == 1
    assert (await q1("SELECT count(*) AS n FROM webhook_deliveries"))["n"] == 1


async def test_status_mapping_from_the_inquiry(api, seed, auth_headers, faspay):
    expired = await make_retail(api, seed, auth_headers)
    faspay.set(INQUIRY, legacy_inquiry_reply("7"))
    notification = legacy_notification(expired["bill_no"], trx_id=expired["trx_id"], status="7")
    assert (await post(api, notification)).status_code == 200
    assert await status_of(expired["payment_id"]) == "EXPIRED"

    unclear = await make_retail(api, seed, auth_headers)
    faspay.set(INQUIRY, legacy_inquiry_reply("9"))  # meaning unconfirmed: record and re-inquire
    resp = await post(
        api, legacy_notification(unclear["bill_no"], trx_id=unclear["trx_id"], status="9")
    )
    assert resp.status_code == 200
    assert await status_of(unclear["payment_id"]) == "PENDING"
    jobs = await q("SELECT job_type FROM gateway_jobs WHERE job_type='INQUIRE_PAYMENT'")
    assert len(jobs) == 1


async def test_ack_code_policy_is_configurable(api, seed, auth_headers, faspay):
    payment = await make_retail(api, seed, auth_headers)
    reply = legacy_inquiry_reply("2")
    reply["response_code"] = "00"
    faspay.set(INQUIRY, reply)
    get_settings().LEGACY_ACK_USE_INQUIRY_CODE = False
    resp = await post(api, legacy_notification(payment["bill_no"], trx_id=payment["trx_id"]))
    assert resp.json()["response_code"] == "00"


async def test_malformed_and_unknown_merchant(api, seed, auth_headers, faspay):
    payment = await make_retail(api, seed, auth_headers)
    assert (await api.post(PATH, content=b"{nope")).status_code == 400
    bad = legacy_notification(payment["bill_no"], trx_id=payment["trx_id"])
    del bad["bill_no"]
    assert (await post(api, bad)).status_code == 400
    other = await post(
        api,
        legacy_notification(payment["bill_no"], trx_id=payment["trx_id"], merchant_id="00000"),
    )
    assert other.json()["response_code"] == "99"
    assert await status_of(payment["payment_id"]) == "PENDING"


async def test_optional_notification_signature_check(api, seed, auth_headers, faspay):
    get_settings().LEGACY_VERIFY_NOTIFICATION_SIGNATURE = True
    payment = await make_retail(api, seed, auth_headers)
    faspay.set(INQUIRY, legacy_inquiry_reply("2"))
    notification = legacy_notification(payment["bill_no"], trx_id=payment["trx_id"])

    wrong = await post(api, notification)
    assert wrong.status_code == 401

    inner = hashlib.md5(f"bot123pass456{payment['bill_no']}2".encode()).hexdigest()  # noqa: S324
    notification["signature"] = hashlib.sha1(inner.encode()).hexdigest()  # noqa: S324
    ok = await post(api, notification)
    assert ok.status_code == 200
    assert await status_of(payment["payment_id"]) == "SUCCEEDED"
    event = await q1(
        "SELECT signature_valid FROM webhook_events WHERE processing_status='PROCESSED'"
    )
    assert event["signature_valid"] is True
