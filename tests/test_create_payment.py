from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from tests.helpers import SNAP_OK, create, payment_body, q, q1, run

pytestmark = pytest.mark.integration


async def test_create_va_snap(api, seed, auth_headers, faspay):
    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers, payment_body(merchant_reference="INV-1001"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["merchant_reference"] == "INV-1001"
    assert body["amount"] == 150000 and body["currency"] == "IDR"
    assert body["provider_reference"] == "8888800000001"  # padding stripped
    instruction = body["payment_instruction"]
    assert instruction["type"] == "VA"
    assert instruction["virtual_account_no"] == "8888800000001"
    assert instruction["redirect_url"] == "https://faspay.test/redirect/va"
    assert instruction["qr_url"] is None and instruction["payment_code"] is None
    assert "_snap" not in instruction  # internal hints never leak
    assert body["metadata"] == {"platform": "CROWDFUNDING", "program_id": "123"}
    assert "protocol" not in body

    (call,) = faspay.calls_to("/v1.0/transfer-va/create-va")
    assert call.signature_ok is True  # signed per the documented formula
    assert call.headers["x-partner-id"] == "P1"
    assert call.headers["channel-id"] == "77001"
    assert call.headers["x-external-id"]
    sent = call.body
    assert sent["totalAmount"] == {"value": "150000.00", "currency": "IDR"}
    assert "virtualAccountNo" not in sent  # Faspay assigns the VA number
    assert sent["trxId"] != "INV-1001"  # never the consumer's reference
    assert len(sent["trxId"]) == 16
    assert sent["additionalInfo"]["channelCode"] == "402"

    payment = await q1("SELECT * FROM payment_transactions")
    assert payment["status"] == "PENDING"
    assert payment["protocol"] == "SNAP"
    assert payment["provider_partner_reference"] == sent["trxId"]
    events = await q("SELECT event_type, source FROM payment_events ORDER BY created_at, id")
    assert [e["event_type"] for e in events] == ["payment.created", "payment.pending"]
    assert await q("SELECT 1 FROM gateway_jobs") == []  # CREATE-sourced events are not delivered
    attempt = await q1("SELECT * FROM payment_attempts")
    assert attempt["operation"] == "CREATE" and attempt["outcome"] == "SUCCEEDED"
    # Customer details are used for the provider call only and are not stored.
    stored = await q1("SELECT metadata, instruction FROM payment_transactions")
    assert "Contoh" not in str(stored)


async def test_create_qris_ewallet_retail(api, seed, auth_headers, faspay):
    headers = await auth_headers(api, seed.cf)

    qris = await create(api, headers, payment_body(payment_method="QRIS", channel_code="715"))
    assert qris.status_code == 201, qris.text
    assert qris.json()["payment_instruction"]["qr_url"] == "https://faspay.test/qr/1.png"
    assert qris.json()["provider_reference"] == "QR-REF-1"
    qr_sent = faspay.calls_to("/v1.0/qr/qr-mpm-generate")[0].body
    assert qr_sent["merchantId"] == "12345"
    assert qr_sent["amount"]["value"] == "150000.00"
    assert qr_sent["validityPeriod"].endswith("+07:00")

    wallet = await create(api, headers, payment_body(payment_method="EWALLET", channel_code="722"))
    assert wallet.status_code == 201, wallet.text
    assert wallet.json()["payment_instruction"]["redirect_url"] == "https://faspay.test/wallet/1"

    retail = await create(api, headers, payment_body(payment_method="RETAIL", channel_code="707"))
    assert retail.status_code == 201, retail.text
    instruction = retail.json()["payment_instruction"]
    assert instruction["payment_code"] == "3004445550000123"
    assert instruction["redirect_url"] == "https://faspay.test/retail/1"
    legacy_sent = faspay.calls_to("/cvr/300011/10")[0].body
    assert legacy_sent["bill_total"] == "15000000"  # rupiah x 100
    assert legacy_sent["bill_no"] == legacy_sent["bill_reff"]
    assert legacy_sent["payment_channel"] == "707"
    assert legacy_sent["signature"]
    assert (await q1("SELECT protocol FROM payment_transactions WHERE payment_method='RETAIL'"))[
        "protocol"
    ] == "LEGACY"


async def test_validation_and_auth_errors(api, seed, auth_headers):
    headers = await auth_headers(api, seed.cf)
    body = payment_body()

    resp = await api.post("/v1/payments", json=body, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert resp.json()["error"]["correlation_id"]

    resp = await api.post(
        "/v1/payments", json=body, headers={**headers, "Idempotency-Key": "x" * 129}
    )
    assert resp.status_code == 400

    for bad in ({"amount": 1500.5}, {"amount": "150000"}, {"amount": 0}, {"amount": -5}):
        resp = await create(api, headers, payment_body(**bad))
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert (await create(api, headers, payment_body(currency="USD"))).status_code == 422
    assert (await create(api, headers, payment_body(payment_method="CARD"))).status_code == 422
    assert (await create(api, headers, payment_body(merchant_reference=""))).status_code == 422
    assert (
        await create(api, headers, payment_body(metadata={f"k{i}": "v" for i in range(21)}))
    ).status_code == 422

    assert (
        await api.post(
            "/v1/payments",
            content=b"{not json",
            headers={**headers, "Idempotency-Key": "k", "Content-Type": "application/json"},
        )
    ).status_code == 400
    assert (
        await api.post("/v1/payments", json=body, headers={"Idempotency-Key": "k"})
    ).status_code == 401


async def test_validation_never_echoes_input(api, seed, auth_headers):
    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers, payment_body(customer={"email": "not-an-email-secret-xyz"}))
    assert resp.status_code == 422
    assert "secret-xyz" not in resp.text


async def test_scope_and_route_authorization(api, seed, auth_headers):
    read_only = await auth_headers(api, seed.cf, scope="payments:read")
    resp = await create(api, read_only)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "INSUFFICIENT_SCOPE"

    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers, payment_body(channel_code="999"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ROUTE_NOT_AUTHORIZED"

    # A route that belongs to the production merchant is unusable in a sandbox deployment.
    resp = await create(
        api, headers, payment_body(payment_method="QRIS", channel_code="715", route_code="PROD")
    )
    assert resp.status_code == 403

    await run("UPDATE merchant_accounts SET status='MAINTENANCE' WHERE merchant_code='KRN-SBX'")
    from app.services.routing import reset_route_cache

    reset_route_cache()
    resp = await create(api, headers)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "MERCHANT_MAINTENANCE"


async def test_ignored_fields_cannot_choose_client_or_destination(api, seed, auth_headers, faspay):
    headers = await auth_headers(api, seed.cf)
    body = payment_body(
        client_id=str(seed.fh.id), app_code="FLUENCY_HUB", callback_url="https://evil.test/hook"
    )
    resp = await create(api, headers, body)
    assert resp.status_code == 201
    row = await q1("SELECT client_id FROM payment_transactions")
    assert row["client_id"] == seed.cf.id


async def test_idempotent_replay_and_conflict(api, seed, auth_headers, faspay):
    headers = await auth_headers(api, seed.cf)
    body = payment_body(merchant_reference="INV-IDEM")
    first = await create(api, headers, body, key="key-1")
    assert first.status_code == 201

    replay = await create(api, headers, body, key="key-1")
    assert replay.status_code == 200
    assert replay.json()["payment_id"] == first.json()["payment_id"]
    assert len(faspay.calls_to("/v1.0/transfer-va/create-va")) == 1  # provider called once

    changed = await create(api, headers, {**body, "amount": 99000}, key="key-1")
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert (await q1("SELECT count(*) AS n FROM payment_transactions"))["n"] == 1


async def test_concurrent_same_key_creates_one_payment(api, seed, auth_headers, faspay):
    headers = await auth_headers(api, seed.cf)
    body = payment_body(merchant_reference="INV-RACE")
    responses = await asyncio.gather(
        *[create(api, headers, body, key="race-key") for _ in range(6)]
    )
    assert {r.status_code for r in responses} <= {200, 201, 202}
    assert len({r.json()["payment_id"] for r in responses}) == 1
    assert (await q1("SELECT count(*) AS n FROM payment_transactions"))["n"] == 1
    assert len(faspay.calls_to("/v1.0/transfer-va/create-va")) == 1


async def test_one_live_payment_per_reference(api, seed, auth_headers, faspay):
    headers = await auth_headers(api, seed.cf)
    first = await create(api, headers, payment_body(merchant_reference="INV-DUP"))
    assert first.status_code == 201
    other_key = await create(api, headers, payment_body(merchant_reference="INV-DUP"))
    assert other_key.status_code == 409
    assert other_key.json()["error"]["code"] == "MERCHANT_REFERENCE_CONFLICT"
    # The failed attempt left no idempotency record behind.
    assert (await q1("SELECT count(*) AS n FROM api_idempotency_keys"))["n"] == 1

    # After a final failure/expiry the same reference starts a new payment.
    await run("UPDATE payment_transactions SET status='EXPIRED'")
    again = await create(api, headers, payment_body(merchant_reference="INV-DUP"))
    assert again.status_code == 201
    assert again.json()["payment_id"] != first.json()["payment_id"]
    latest = await api.get("/v1/payments/by-reference/INV-DUP", headers=headers)
    assert latest.json()["payment_id"] == again.json()["payment_id"]
    # ...while the earlier one stays reachable by id.
    old = await api.get(f"/v1/payments/{first.json()['payment_id']}", headers=headers)
    assert old.status_code == 200 and old.json()["status"] == "EXPIRED"

    # After SUCCEEDED the reference is closed.
    await run("UPDATE payment_transactions SET status='SUCCEEDED' WHERE status='PENDING'")
    assert (
        await create(api, headers, payment_body(merchant_reference="INV-DUP"))
    ).status_code == 409


async def test_same_reference_for_two_clients_on_a_shared_merchant(api, seed, auth_headers, faspay):
    cf, fh = await auth_headers(api, seed.cf), await auth_headers(api, seed.fh)
    one = await create(api, cf, payment_body(merchant_reference="SHARED-1"))
    two = await create(api, fh, payment_body(merchant_reference="SHARED-1"))
    assert one.status_code == 201 and two.status_code == 201
    assert one.json()["payment_id"] != two.json()["payment_id"]


async def test_read_is_scoped_to_the_client(api, seed, auth_headers, faspay):
    cf, fh = await auth_headers(api, seed.cf), await auth_headers(api, seed.fh)
    created = (await create(api, cf, payment_body(merchant_reference="PRIVATE-1"))).json()

    mine = await api.get(f"/v1/payments/{created['payment_id']}", headers=cf)
    assert mine.status_code == 200
    assert mine.json()["payment_id"] == created["payment_id"]
    assert mine.json()["paid_at"] is None and mine.json()["expires_at"]

    other = await api.get(f"/v1/payments/{created['payment_id']}", headers=fh)
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "PAYMENT_NOT_FOUND"
    assert (await api.get("/v1/payments/by-reference/PRIVATE-1", headers=fh)).status_code == 404
    assert (await api.get("/v1/payments/not-a-uuid", headers=cf)).status_code == 404
    assert (await api.get(f"/v1/payments/{uuid.uuid4()}", headers=cf)).status_code == 404


async def test_expiry_defaults_and_limits(api, seed, auth_headers, faspay):
    headers = await auth_headers(api, seed.cf)
    snap = (await create(api, headers)).json()
    legacy = (
        await create(api, headers, payment_body(payment_method="RETAIL", channel_code="707"))
    ).json()
    rows = {
        r["payment_method"]: r
        for r in await q("SELECT payment_method, expires_at, created_at FROM payment_transactions")
    }
    snap_minutes = (rows["VA"]["expires_at"] - rows["VA"]["created_at"]).total_seconds() / 60
    legacy_minutes = (
        rows["RETAIL"]["expires_at"] - rows["RETAIL"]["created_at"]
    ).total_seconds() / 60
    assert 299 < snap_minutes < 301  # SNAP default: 5 hours
    assert 1439 < legacy_minutes < 1441  # Legacy default: 1 day
    assert snap["payment_instruction"]["expires_at"] and legacy["expires_at"]

    too_soon = await create(api, headers, payment_body(expires_at="2020-01-01T00:00:00+07:00"))
    assert too_soon.status_code == 422
    assert too_soon.json()["error"]["code"] == "EXPIRES_AT_TOO_SOON"
    far = await create(api, headers, payment_body(expires_at="2099-01-01T00:00:00+07:00"))
    assert far.json()["error"]["code"] == "EXPIRES_AT_TOO_FAR"
    naive = await create(api, headers, payment_body(expires_at="2030-01-01T00:00:00"))
    assert naive.status_code == 422  # a timestamp without an offset is rejected


async def test_provider_rejection_is_a_confirmed_failure(api, seed, auth_headers, faspay):
    faspay.set(
        "/v1.0/transfer-va/create-va",
        {"responseCode": "4002701", "responseMessage": "Invalid Field Format"},
        status=400,
    )
    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers, payment_body(merchant_reference="INV-F"), key="fail-key")
    assert resp.status_code == 502
    error = resp.json()["error"]
    assert error["code"] == "PROVIDER_REJECTED"
    assert error["status"] == "FAILED" and error["payment_id"]
    assert (await q1("SELECT status FROM payment_transactions"))["status"] == "FAILED"
    attempt = await q1("SELECT outcome, provider_response_code FROM payment_attempts")
    assert attempt == {"outcome": "FAILED", "provider_response_code": "4002701"}

    replay = await create(api, headers, payment_body(merchant_reference="INV-F"), key="fail-key")
    assert replay.status_code == 502  # the same key keeps returning the original result
    assert len(faspay.calls_to("/v1.0/transfer-va/create-va")) == 1
    # A new key after a final failure may create a fresh payment for the same reference.
    faspay.set("/v1.0/transfer-va/create-va", None)
    faspay._overrides.pop("/v1.0/transfer-va/create-va")
    retry = await create(api, headers, payment_body(merchant_reference="INV-F"), key="new-key")
    assert retry.status_code == 201


async def test_connect_error_is_confirmed_not_sent(api, seed, auth_headers, faspay):
    faspay.set("/v1.0/qr/qr-mpm-generate", exc=httpx.ConnectError("refused"))
    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers, payment_body(payment_method="QRIS", channel_code="715"))
    assert resp.status_code == 502
    assert (await q1("SELECT status FROM payment_transactions"))["status"] == "FAILED"


@pytest.mark.parametrize(
    "scenario",
    [
        {"exc": httpx.ReadTimeout("slow")},
        {"status": 500, "response": {"responseCode": "5002700", "responseMessage": "boom"}},
        {"status": 409, "response": {"responseCode": "4092700", "responseMessage": "dup"}},
        {"status": 200, "response": {"responseCode": "2004700", "responseMessage": "ok"}},
    ],
    ids=["timeout", "http-500", "duplicate-409", "success-without-reference"],
)
async def test_ambiguous_outcomes_become_unknown_and_never_resubmit(
    api, seed, auth_headers, faspay, scenario
):
    faspay.set("/v1.0/qr/qr-mpm-generate", **scenario)
    headers = await auth_headers(api, seed.cf)
    body = payment_body(payment_method="QRIS", channel_code="715", merchant_reference="INV-U")
    resp = await create(api, headers, body, key="unk-key")
    assert resp.status_code == 202
    assert resp.json()["status"] == "UNKNOWN"
    assert resp.json()["correlation_id"]

    payment = await q1("SELECT status, protocol FROM payment_transactions")
    assert payment["status"] == "UNKNOWN"
    attempt = await q1("SELECT outcome FROM payment_attempts")
    assert attempt["outcome"] == "UNKNOWN"
    job = await q1("SELECT job_type, status FROM gateway_jobs")
    assert job == {"job_type": "INQUIRE_PAYMENT", "status": "PENDING"}
    idem = await q1("SELECT operation_status FROM api_idempotency_keys")
    assert idem["operation_status"] == "UNKNOWN"

    # A replay returns the same payment and does not call the provider again.
    again = await create(api, headers, body, key="unk-key")
    assert again.status_code == 202
    assert again.json()["payment_id"] == resp.json()["payment_id"]
    assert len(faspay.calls_to("/v1.0/qr/qr-mpm-generate")) == 1
    # And a different key cannot create a second live payment for the reference.
    assert (await create(api, headers, body, key="other")).status_code == 409


async def test_unknown_snap_va_has_no_inquiry_job(api, seed, auth_headers, faspay):
    faspay.set("/v1.0/transfer-va/create-va", exc=httpx.ReadTimeout("slow"))
    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers)
    assert resp.status_code == 202
    # The VA number is unknown, so no inquiry is possible: recovery relies on the callback.
    assert await q("SELECT 1 FROM gateway_jobs") == []


async def test_missing_merchant_credentials_fail_cleanly(
    api, seed, auth_headers, faspay, monkeypatch
):
    monkeypatch.delenv("KRN_PRIVATE_KEY")
    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers)
    assert resp.status_code == 502
    assert faspay.calls == []  # nothing was sent
    assert (await q1("SELECT status FROM payment_transactions"))["status"] == "FAILED"


async def test_cancel_is_not_exposed_yet(api, seed, auth_headers):
    headers = await auth_headers(api, seed.cf)
    resp = await api.post(f"/v1/payments/{uuid.uuid4()}/cancel", headers=headers)
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "CANCEL_NOT_SUPPORTED"


async def test_health_endpoints(api):
    assert (await api.get("/health")).json() == {"status": "ok"}
    assert (await api.get("/ready")).status_code == 200
    resp = await api.get("/health", headers={"X-Correlation-ID": "corr_abcdef123456"})
    assert resp.headers["x-correlation-id"] == "corr_abcdef123456"
    assert SNAP_OK  # keep the import used
