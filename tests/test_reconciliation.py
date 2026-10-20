from __future__ import annotations

import json

import httpx
import pytest

from app.config import get_settings
from app.services import reconciliation
from app.services.job_runner import run_jobs
from tests.helpers import (
    create,
    legacy_inquiry_reply,
    payment_body,
    q,
    q1,
    run,
    snap_status_reply,
)

pytestmark = pytest.mark.integration

QR_QUERY = "/v1.0/qr/qr-mpm-query"
LEGACY_INQUIRY = "/cvr/100004/10"


async def unknown_qris(api, seed, auth_headers, faspay) -> dict:
    faspay.set("/v1.0/qr/qr-mpm-generate", exc=httpx.ReadTimeout("lost"))
    headers = await auth_headers(api, seed.cf)
    resp = await create(
        api, headers, payment_body(payment_method="QRIS", channel_code="715"), key="u-1"
    )
    assert resp.status_code == 202
    # Faspay did create it; the inquiry finds it (partner reference only for QRIS lookups).
    return resp.json()


async def status_of() -> str:
    return (await q1("SELECT status FROM payment_transactions"))["status"]


async def test_unknown_create_resolved_to_pending_by_inquiry(api, seed, auth_headers, faspay):
    payment = await unknown_qris(api, seed, auth_headers, faspay)
    faspay.set(QR_QUERY, snap_status_reply("01"))
    summary = await run_jobs()
    assert summary.done == 1
    assert await status_of() == "PENDING"
    events = await q("SELECT event_type, source FROM payment_events ORDER BY created_at, id")
    assert [e["event_type"] for e in events] == [
        "payment.created",
        "payment.unknown",
        "payment.pending",
    ]
    assert events[-1]["source"] == "INQUIRY"
    # Unknown/pending are recorded; the consumer is only told about payment.unknown
    # transitions after creation, and none of these came after creation.
    assert await q("SELECT 1 FROM webhook_deliveries") == []
    idem = await q1("SELECT operation_status FROM api_idempotency_keys")
    assert idem["operation_status"] == "COMPLETED"
    attempt = await q1("SELECT operation, outcome FROM payment_attempts WHERE operation='INQUIRY'")
    assert attempt == {"operation": "INQUIRY", "outcome": "SUCCEEDED"}
    assert payment["status"] == "UNKNOWN"


async def test_unknown_create_that_actually_succeeded(api, seed, auth_headers, faspay, consumer):
    await unknown_qris(api, seed, auth_headers, faspay)
    faspay.set(
        QR_QUERY,
        {**snap_status_reply("00", amount=150000), "referenceNo": "QR-LATE-1"},
    )
    await run_jobs()
    row = await q1("SELECT status, paid_at, provider_reference FROM payment_transactions")
    assert row["status"] == "SUCCEEDED" and row["paid_at"]
    assert row["provider_reference"] is None or row["provider_reference"] == "QR-LATE-1"
    delivery = await q1("SELECT delivery_status FROM webhook_deliveries")
    assert delivery["delivery_status"] == "DELIVERED"  # payment.succeeded is delivered
    assert json.loads(consumer.requests[0].content)["event_type"] == "payment.succeeded"
    event = await q1("SELECT source FROM payment_events WHERE event_type='payment.succeeded'")
    assert event["source"] == "INQUIRY"


async def test_inquiry_failures_retry_and_never_guess(api, seed, auth_headers, faspay):
    await unknown_qris(api, seed, auth_headers, faspay)
    faspay.set(QR_QUERY, {"responseCode": "5005100"}, status=500)
    assert (await run_jobs()).retried == 1
    faspay.set(QR_QUERY, {"responseCode": "4045101", "responseMessage": "not found"}, status=404)
    await run("UPDATE gateway_jobs SET run_after = now()")
    assert (await run_jobs()).retried == 1
    assert await status_of() == "UNKNOWN"  # never auto-failed, never resubmitted
    job = await q1("SELECT last_error_category FROM gateway_jobs")
    assert job["last_error_category"] == "inquiry_not_found"
    assert len(faspay.calls_to("/v1.0/qr/qr-mpm-generate")) == 1


async def test_inquiry_amount_mismatch_is_not_applied(api, seed, auth_headers, faspay):
    await unknown_qris(api, seed, auth_headers, faspay)
    faspay.set(QR_QUERY, snap_status_reply("00", amount=1))
    await run_jobs()
    await run("UPDATE gateway_jobs SET run_after = now()")
    await run_jobs()
    assert await status_of() == "UNKNOWN"


async def test_legacy_unknown_create_is_resolved_through_the_legacy_inquiry(
    api, seed, auth_headers, faspay
):
    faspay.set("/cvr/300011/10", exc=httpx.ReadTimeout("lost"))
    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers, payment_body(payment_method="RETAIL", channel_code="707"))
    assert resp.status_code == 202
    # For Legacy, trx_id is unknown after a lost create, but the inquiry keys on bill_no too.
    await run("UPDATE payment_transactions SET provider_reference = '3004445550000123'")
    faspay.set(LEGACY_INQUIRY, legacy_inquiry_reply("2"))
    await run_jobs()
    assert await status_of() == "SUCCEEDED"


async def test_stale_created_payment_becomes_unknown(api, seed, auth_headers, faspay):
    headers = await auth_headers(api, seed.cf)
    await create(api, headers, payment_body(payment_method="QRIS", channel_code="715"), key="k")
    await run(
        "UPDATE payment_transactions SET status='CREATED', "
        "updated_at = now() - interval '10 minutes'"
    )
    await run("UPDATE api_idempotency_keys SET operation_status='IN_PROGRESS'")
    counts = await reconciliation.scan_and_enqueue()
    assert counts["created_to_unknown"] == 1
    assert await status_of() == "UNKNOWN"
    assert (await q1("SELECT operation_status FROM api_idempotency_keys"))[
        "operation_status"
    ] == "UNKNOWN"
    # A fresh CREATED payment (provider call in flight) is left alone.
    await run("UPDATE payment_transactions SET status='CREATED', updated_at = now()")
    assert (await reconciliation.scan_and_enqueue())["created_to_unknown"] == 0


async def test_scan_creates_inquiry_jobs_once_per_window(api, seed, auth_headers, faspay):
    await unknown_qris(api, seed, auth_headers, faspay)
    await run("DELETE FROM gateway_jobs")
    first = await reconciliation.scan_and_enqueue()
    second = await reconciliation.scan_and_enqueue()
    assert first["inquire"] == 1 and second["inquire"] == 0  # deduplicated by dedupe_key
    assert (await q1("SELECT count(*) AS n FROM gateway_jobs"))["n"] == 1


async def test_scan_skips_unknown_snap_va_without_a_reference(api, seed, auth_headers, faspay):
    faspay.set("/v1.0/transfer-va/create-va", exc=httpx.ReadTimeout("lost"))
    headers = await auth_headers(api, seed.cf)
    await create(api, headers, payment_body())
    counts = await reconciliation.scan_and_enqueue()
    assert counts["inquire"] == 0  # the VA number is unknown: only the callback can recover it


async def pending_qris_past_expiry(api, seed, auth_headers, faspay, minutes_past: int = 60):
    headers = await auth_headers(api, seed.cf)
    resp = await create(api, headers, payment_body(payment_method="QRIS", channel_code="715"))
    assert resp.status_code == 201
    await run(
        "UPDATE payment_transactions SET expires_at = now() - make_interval(mins => :m)",
        m=minutes_past,
    )


async def test_expiry_is_confirmed_by_the_provider(api, seed, auth_headers, faspay):
    await pending_qris_past_expiry(api, seed, auth_headers, faspay)
    assert (await reconciliation.scan_and_enqueue())["expire"] == 1

    faspay.set(QR_QUERY, snap_status_reply("01"))  # provider says: still pending
    summary = await run_jobs()
    assert summary.retried == 1
    assert await status_of() == "PENDING"  # not expired without confirmation
    assert (await q1("SELECT last_error_category FROM gateway_jobs"))[
        "last_error_category"
    ] == "expiry_not_confirmed"

    faspay.set(QR_QUERY, snap_status_reply("07"))
    await run("UPDATE gateway_jobs SET run_after = now()")
    assert (await run_jobs()).done == 1
    assert await status_of() == "EXPIRED"
    event = await q1("SELECT source FROM payment_events WHERE event_type='payment.expired'")
    assert event["source"] == "EXPIRY"
    assert (
        await q1("SELECT job_type FROM gateway_jobs WHERE job_type='DELIVER_WEBHOOK'")
    ) is not None


async def test_expiry_threshold_is_per_method(api, seed, auth_headers, faspay):
    # QRIS waits 15 minutes past expiry; at 5 minutes past nothing is queued yet.
    await pending_qris_past_expiry(api, seed, auth_headers, faspay, minutes_past=5)
    assert (await reconciliation.scan_and_enqueue())["expire"] == 0


async def test_paid_but_missed_callback_is_found_at_expiry(api, seed, auth_headers, faspay):
    await pending_qris_past_expiry(api, seed, auth_headers, faspay)
    await reconciliation.scan_and_enqueue()
    faspay.set(QR_QUERY, snap_status_reply("00", amount=150000))
    await run_jobs()
    assert await status_of() == "SUCCEEDED"


async def test_forced_expiry_is_an_explicit_option(api, seed, auth_headers, faspay):
    get_settings().EXPIRY_FORCE_AFTER_MINUTES = 30
    await pending_qris_past_expiry(api, seed, auth_headers, faspay, minutes_past=120)
    await reconciliation.scan_and_enqueue()
    faspay.set(QR_QUERY, snap_status_reply("01"))
    assert (await run_jobs()).done == 1
    assert await status_of() == "EXPIRED"


async def test_manual_reconciliation_endpoint(api, seed, auth_headers, faspay):
    await unknown_qris(api, seed, auth_headers, faspay)
    pid = (await q1("SELECT id FROM payment_transactions"))["id"]
    await run("DELETE FROM gateway_jobs")
    admin = {"Authorization": "Bearer test-admin-token", "X-Operator": "bob"}
    assert (await api.post(f"/internal/reconciliation/{pid}")).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert (await api.post(f"/internal/reconciliation/{pid}", headers=wrong)).status_code == 401
    assert (await api.post("/internal/reconciliation/not-a-uuid", headers=admin)).status_code == 404

    queued = await api.post(f"/internal/reconciliation/{pid}", headers=admin)
    assert queued.status_code == 200 and queued.json()["status"] == "QUEUED"
    assert (await q1("SELECT job_type FROM gateway_jobs"))["job_type"] == "RECONCILE_PAYMENT"

    faspay.set(QR_QUERY, snap_status_reply("01"))
    inline = await api.post(f"/internal/reconciliation/{pid}?inline=true", headers=admin)
    assert inline.json()["outcome"] == "RESOLVED" and inline.json()["status"] == "PENDING"
    audits = await q("SELECT actor_type, actor_id, action FROM audit_logs ORDER BY created_at")
    assert [a["actor_id"] for a in audits] == ["bob", "bob"]
    assert all(a["actor_type"] == "ADMIN" and a["action"] == "reconcile" for a in audits)


async def test_reprocess_endpoint_applies_a_held_event(api, seed, auth_headers, faspay):
    from tests.helpers import minified, sign_snap_callback, va_notification

    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    headers = await auth_headers(api, seed.cf)
    await create(api, headers, payment_body())
    ref = (await q1("SELECT provider_partner_reference AS r FROM payment_transactions"))["r"]
    await run("UPDATE payment_transactions SET status='CREATED'")
    path = "/v1.0/transfer-va/payment"
    body = minified(va_notification(ref))
    held = await api.post(path, content=body, headers=sign_snap_callback(path, body))
    assert held.status_code == 404
    event_id = (await q1("SELECT id FROM webhook_events"))["id"]

    admin = {"Authorization": "Bearer test-admin-token"}
    await run("UPDATE payment_transactions SET status='PENDING'")
    resp = await api.post(f"/internal/webhook-events/{event_id}/reprocess", headers=admin)
    assert resp.status_code == 200 and resp.json()["outcome"] == "APPLIED"
    assert await status_of() == "SUCCEEDED"
    again = await api.post(f"/internal/webhook-events/{event_id}/reprocess", headers=admin)
    assert again.json()["outcome"] == "DUPLICATE"
    assert (await api.post(f"/internal/webhook-events/{event_id}/reprocess")).status_code == 401


async def test_retention_purges_support_data_but_never_the_financial_record(
    api, seed, auth_headers, faspay
):
    from tests.helpers import minified, sign_snap_callback, va_notification

    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    headers = await auth_headers(api, seed.cf)
    await create(api, headers, payment_body(), key="old-key")
    ref = (await q1("SELECT provider_partner_reference AS r FROM payment_transactions"))["r"]
    path = "/v1.0/transfer-va/payment"
    body = minified(va_notification(ref))
    await api.post(path, content=body, headers=sign_snap_callback(path, body))
    await api.post(path, content=b"{bad", headers={"Content-Type": "application/json"})  # REJECTED

    for table, column in (
        ("webhook_events", "received_at"),
        ("gateway_jobs", "created_at"),
        ("payment_attempts", "started_at"),
        ("webhook_deliveries", "created_at"),
        ("audit_logs", "created_at"),
    ):
        await run(f"UPDATE {table} SET {column} = now() - interval '400 days'")
    await run("UPDATE gateway_jobs SET status='DONE', finished_at = now() - interval '40 days'")
    await run(
        "UPDATE api_idempotency_keys SET resolved_at = now() - interval '40 days', "
        "operation_status='COMPLETED'"
    )
    await run(
        "INSERT INTO audit_logs (id, entity_type, entity_id, actor_type, action, created_at) "
        "VALUES (gen_random_uuid(), 'ROUTE', 'x', 'ADMIN', 'old', now() - interval '400 days')"
    )

    await reconciliation.handle_purge_retention({})
    assert (await q1("SELECT count(*) AS n FROM payment_transactions"))["n"] == 1
    assert (await q1("SELECT count(*) AS n FROM payment_events"))["n"] >= 2
    assert (
        await q1("SELECT count(*) AS n FROM webhook_events WHERE processing_status='REJECTED'")
    )["n"] == 0
    processed = await q1(
        "SELECT payload_redacted FROM webhook_events WHERE processing_status='PROCESSED'"
    )
    assert processed["payload_redacted"] is None  # payload dropped, the row (and FK) kept
    assert (await q1("SELECT count(*) AS n FROM gateway_jobs"))["n"] == 0
    assert (await q1("SELECT count(*) AS n FROM api_idempotency_keys"))["n"] == 0
    assert (await q1("SELECT count(*) AS n FROM payment_attempts"))["n"] == 0
    assert (await q1("SELECT count(*) AS n FROM audit_logs WHERE action='old'"))["n"] == 0


async def test_unresolved_idempotency_keys_are_never_purged(api, seed, auth_headers, faspay):
    await unknown_qris(api, seed, auth_headers, faspay)
    await run("UPDATE api_idempotency_keys SET resolved_at = now() - interval '999 days'")
    await reconciliation.handle_purge_retention({})
    assert (await q1("SELECT count(*) AS n FROM api_idempotency_keys"))["n"] == 1


async def test_daily_purge_is_scheduled_once_per_day(db):
    assert await reconciliation.enqueue_daily_purge() is True
    assert await reconciliation.enqueue_daily_purge() is False
