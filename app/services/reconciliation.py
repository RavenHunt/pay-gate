"""Inquiry-based resolution, reconciliation, expiry, scans and retention (TRD 12.7, 13)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.adapters.faspay.base import InquiryOutcome, InquiryResult, InquiryTarget
from app.adapters.faspay.registry import get_driver
from app.config import ConfigError, get_settings
from app.database import transaction
from app.domain import FINAL_STATUSES, JobType, PaymentStatus
from app.observability import alert, emit_metric, get_logger
from app.repositories import attempts as attempts_repo
from app.repositories import idempotency as idempotency_repo
from app.repositories import jobs as jobs_repo
from app.repositories import merchants as merchants_repo
from app.repositories import payments as payments_repo
from app.repositories.db import execute, fetch_all
from app.services.job_errors import DeadJob, RetryJob
from app.services.state_machine import can_transition
from app.services.transitions import apply_transition
from app.utils.timeutil import utcnow

log = get_logger("reconcile")


class ReconcileOutcome(StrEnum):
    RESOLVED = "RESOLVED"  # state moved
    CONFIRMED = "CONFIRMED"  # provider agrees with the stored state
    FINAL = "FINAL"  # nothing to do, the payment is final
    STILL_PENDING = "STILL_PENDING"
    NOT_FOUND = "NOT_FOUND"
    NOT_INQUIRABLE = "NOT_INQUIRABLE"
    MISMATCH = "MISMATCH"
    ERROR = "ERROR"


@dataclass
class ReconcileResult:
    outcome: ReconcileOutcome
    status: PaymentStatus | None = None
    job_enqueued: bool = False


def _target_for(payment: dict[str, Any], merchant: dict[str, Any]) -> InquiryTarget:
    instruction = payment.get("instruction") or {}
    return InquiryTarget(
        protocol=payment["protocol"],
        payment_method=payment["payment_method"],
        partner_reference=payment["provider_partner_reference"],
        provider_reference=payment["provider_reference"],
        channel_code=payment["channel_code"],
        merchant=merchant,
        amount=payment["amount"],
        snap_extras=dict(instruction.get("_snap") or {}),
    )


async def inquire_and_apply(payment_id: uuid.UUID, *, source: str) -> ReconcileResult:
    """One provider inquiry for a stored payment, applied through the state machine."""
    protocol = "?"
    async with transaction() as conn:
        payment = await payments_repo.get_by_id(conn, payment_id)
        if payment is None or PaymentStatus(payment["status"]) in FINAL_STATUSES:
            return ReconcileResult(ReconcileOutcome.FINAL)
        merchant = await merchants_repo.get_merchant(conn, payment["merchant_account_id"])
        protocol = payment["protocol"]
        attempt_id = await attempts_repo.start_attempt(
            conn, payment_id=payment_id, operation="INQUIRY", protocol=protocol, external_id=None
        )
    assert merchant is not None

    try:
        result = await get_driver(protocol).inquire_payment(_target_for(payment, merchant))
    except ConfigError:
        alert("merchant_credentials_unavailable", merchant_code=merchant["merchant_code"])
        result = InquiryResult(InquiryOutcome.ERROR, error_category="config_error")
    emit_metric(
        "payment_inquiry",
        protocol=protocol,
        merchant_code=merchant["merchant_code"],
        outcome=result.outcome.value,
    )

    async with transaction() as conn:
        locked = await payments_repo.get_by_id(conn, payment_id, for_update=True)
        assert locked is not None
        await attempts_repo.finish_attempt(
            conn,
            attempt_id,
            outcome=_attempt_outcome(result),
            http_status=result.http_status,
            provider_response_code=result.response_code,
            provider_reference=result.provider_reference,
            error_category=result.error_category,
        )
        return await _apply_inquiry(conn, locked, result, source)


def _attempt_outcome(result: InquiryResult) -> str:
    if result.outcome == InquiryOutcome.OK:
        return "SUCCEEDED"
    return "UNKNOWN" if result.outcome == InquiryOutcome.ERROR else "FAILED"


async def _apply_inquiry(
    conn: AsyncConnection, payment: dict[str, Any], result: InquiryResult, source: str
) -> ReconcileResult:
    current = PaymentStatus(payment["status"])
    if current in FINAL_STATUSES:
        return ReconcileResult(ReconcileOutcome.FINAL)
    if result.outcome == InquiryOutcome.NOT_INQUIRABLE:
        return ReconcileResult(ReconcileOutcome.NOT_INQUIRABLE)
    if result.outcome == InquiryOutcome.NOT_FOUND:
        alert("inquiry_not_found", payment_id=str(payment["id"]), status=current.value)
        return ReconcileResult(ReconcileOutcome.NOT_FOUND)
    if result.outcome != InquiryOutcome.OK:
        return ReconcileResult(ReconcileOutcome.ERROR)

    if result.amount is not None and result.amount != payment["amount"]:
        alert("inquiry_amount_mismatch", payment_id=str(payment["id"]))
        return ReconcileResult(ReconcileOutcome.MISMATCH)

    if payment["provider_reference"] is None and result.provider_reference:
        await payments_repo.set_provider_fields(
            conn,
            payment["id"],
            provider_status=None,
            provider_reference=result.provider_reference,
        )
    if result.status is None:
        await payments_repo.set_provider_fields(
            conn, payment["id"], provider_status=result.provider_status, provider_reference=None
        )
        if result.mapping and result.mapping.alert:
            alert(result.mapping.alert, payment_id=str(payment["id"]), raw=result.provider_status)
        return ReconcileResult(ReconcileOutcome.STILL_PENDING)
    if result.status == current:
        await payments_repo.set_provider_fields(
            conn, payment["id"], provider_status=result.provider_status, provider_reference=None
        )
        return ReconcileResult(ReconcileOutcome.CONFIRMED, current)
    if not can_transition(current, result.status):
        return ReconcileResult(ReconcileOutcome.STILL_PENDING)

    transition = await apply_transition(
        conn,
        payment,
        result.status,
        source=source,
        provider_status=result.provider_status,
        paid_at=(result.paid_at or utcnow()) if result.status == PaymentStatus.SUCCEEDED else None,
    )
    return ReconcileResult(
        ReconcileOutcome.RESOLVED if transition.applied else ReconcileOutcome.CONFIRMED,
        result.status,
        job_enqueued=transition.job_enqueued,
    )


# ── Job handlers ───────────────────────────────────────────────────────


async def handle_inquire_payment(job: dict[str, Any]) -> None:
    result = await inquire_and_apply(job["ref_id"], source="INQUIRY")
    outcome = result.outcome
    if outcome in (
        ReconcileOutcome.ERROR,
        ReconcileOutcome.NOT_FOUND,
        ReconcileOutcome.STILL_PENDING,
    ):
        raise RetryJob(f"inquiry_{outcome.value.lower()}")
    expected = (job.get("args") or {}).get("expect")
    if expected and outcome == ReconcileOutcome.CONFIRMED and result.status != expected:
        # A callback claimed the payment was paid and Faspay has not confirmed it yet.
        raise RetryJob("claimed_paid_not_confirmed")


async def handle_reconcile_payment(job: dict[str, Any]) -> None:
    outcome = (await inquire_and_apply(job["ref_id"], source="RECONCILE")).outcome
    if outcome == ReconcileOutcome.ERROR:
        raise RetryJob("inquiry_error")
    if outcome == ReconcileOutcome.MISMATCH:
        raise DeadJob("amount_mismatch")  # alerted; needs a human


async def handle_expire_payment(job: dict[str, Any]) -> None:
    settings = get_settings()
    payment_id = job["ref_id"]
    outcome = (await inquire_and_apply(payment_id, source="EXPIRY")).outcome
    if outcome in (ReconcileOutcome.FINAL, ReconcileOutcome.RESOLVED):
        return
    if outcome == ReconcileOutcome.MISMATCH:
        raise DeadJob("amount_mismatch")

    force = settings.EXPIRY_FORCE_AFTER_MINUTES
    if force > 0 and await _force_expire(payment_id, force):
        return
    # The provider has not confirmed expiry: stay PENDING and look again later.
    raise RetryJob("expiry_not_confirmed")


async def _force_expire(payment_id: uuid.UUID, grace_minutes: int) -> bool:
    async with transaction() as conn:
        payment = await payments_repo.get_by_id(conn, payment_id, for_update=True)
        if payment is None or payment["status"] != PaymentStatus.PENDING:
            return payment is not None and payment["status"] in FINAL_STATUSES
        deadline = payment["expires_at"] + timedelta(minutes=grace_minutes)
        if payment["expires_at"] is None or utcnow() < deadline:
            return False
        alert("forced_expiry", payment_id=str(payment_id))
        result = await apply_transition(conn, payment, PaymentStatus.EXPIRED, source="EXPIRY")
        return result.applied


# ── Scans (create the recurring job rows) ─────────────────────────────


async def scan_and_enqueue() -> dict[str, int]:
    """Called at the start of a scheduled run. Idempotent through dedupe keys."""
    settings = get_settings()
    counts = {"created_to_unknown": 0, "inquire": 0, "expire": 0}
    limit = settings.SCAN_BATCH_LIMIT

    # 1. A CREATED payment this old lost its create call (function died): UNKNOWN.
    async with transaction() as conn:
        stale = await fetch_all(
            conn,
            """
            SELECT id FROM payment_transactions
            WHERE status = 'CREATED'
              AND updated_at < now() - make_interval(mins => CAST(:mins AS integer))
            ORDER BY updated_at LIMIT :limit FOR UPDATE SKIP LOCKED
            """,
            {"mins": settings.CREATED_STALE_MINUTES, "limit": min(limit, 50)},
        )
    for row in stale:
        async with transaction() as conn:
            payment = await payments_repo.get_by_id(conn, row["id"], for_update=True)
            if payment is None or payment["status"] != PaymentStatus.CREATED:
                continue
            result = await apply_transition(
                conn, payment, PaymentStatus.UNKNOWN, source="RECONCILE"
            )
            if result.applied:
                await idempotency_repo.mark_unknown_for_payment(conn, payment["id"])
                counts["created_to_unknown"] += 1
                alert("create_lost_marked_unknown", payment_id=str(payment["id"]))

    async with transaction() as conn:
        # 2. UNKNOWN payments that can be inquired.
        counts["inquire"] = await execute(
            conn,
            """
            INSERT INTO gateway_jobs
              (id, job_type, status, dedupe_key, ref_id, attempts, max_attempts, run_after)
            SELECT gen_random_uuid(), 'INQUIRE_PAYMENT', 'PENDING',
                   'inquire:' || p.id || ':' || to_char(now(), 'YYYYMMDDHH24'),
                   p.id, 0, :max_attempts, now()
            FROM payment_transactions p
            WHERE p.status = 'UNKNOWN'
              AND NOT (p.protocol = 'SNAP' AND p.payment_method = 'VA'
                       AND p.provider_reference IS NULL)
              AND p.updated_at < now() - make_interval(secs => CAST(:delay AS double precision))
            LIMIT :limit
            ON CONFLICT (dedupe_key) DO NOTHING
            """,
            {
                "max_attempts": settings.INQUIRY_MAX_ATTEMPTS,
                "delay": float(settings.UNKNOWN_INQUIRY_DELAY_SECONDS),
                "limit": limit,
            },
        )
        # 3. PENDING payments past expiry plus the per-method stale threshold.
        thresholds = {
            m: settings.STALE_PENDING_MINUTES_BY_METHOD.get(m, 60)
            for m in ("QRIS", "EWALLET", "VA", "RETAIL")
        }
        counts["expire"] = await execute(
            conn,
            """
            INSERT INTO gateway_jobs
              (id, job_type, status, dedupe_key, ref_id, attempts, max_attempts, run_after)
            SELECT gen_random_uuid(), 'EXPIRE_PAYMENT', 'PENDING',
                   'expire:' || p.id || ':' || to_char(now(), 'YYYYMMDDHH24'),
                   p.id, 0, :max_attempts, now()
            FROM payment_transactions p
            WHERE p.status = 'PENDING' AND p.expires_at IS NOT NULL
              AND p.expires_at + make_interval(mins => CASE p.payment_method
                    WHEN 'QRIS' THEN CAST(:qris AS integer)
                    WHEN 'EWALLET' THEN CAST(:ewallet AS integer)
                    WHEN 'VA' THEN CAST(:va AS integer)
                    ELSE CAST(:retail AS integer) END) < now()
            LIMIT :limit
            ON CONFLICT (dedupe_key) DO NOTHING
            """,
            {
                "max_attempts": settings.INQUIRY_MAX_ATTEMPTS,
                "qris": thresholds["QRIS"],
                "ewallet": thresholds["EWALLET"],
                "va": thresholds["VA"],
                "retail": thresholds["RETAIL"],
                "limit": limit,
            },
        )
    return counts


async def enqueue_daily_purge() -> bool:
    async with transaction() as conn:
        job_id = await jobs_repo.enqueue(
            conn,
            JobType.PURGE_RETENTION,
            dedupe_key=f"purge:{utcnow().strftime('%Y%m%d')}",
            max_attempts=3,
        )
    return job_id is not None


# ── Retention ──────────────────────────────────────────────────────────

_PURGES: list[tuple[str, str]] = [
    (
        "webhook_event_payloads",
        """
        UPDATE webhook_events SET payload_redacted = NULL
        WHERE ctid IN (
            SELECT ctid FROM webhook_events
            WHERE processing_status = 'PROCESSED' AND payload_redacted IS NOT NULL
              AND received_at < now() - make_interval(days => CAST(:short_days AS integer))
              LIMIT :limit)
        """,
    ),
    (
        "rejected_events",
        """
        DELETE FROM webhook_events WHERE ctid IN (
            SELECT ctid FROM webhook_events
            WHERE processing_status = 'REJECTED'
              AND received_at < now() - make_interval(days => CAST(:short_days AS integer))
              LIMIT :limit)
        """,
    ),
    (
        "old_review_events",
        """
        DELETE FROM webhook_events WHERE ctid IN (
            SELECT ctid FROM webhook_events
            WHERE processing_status IN ('UNMATCHED', 'FAILED')
              AND received_at < now() - make_interval(days => CAST(:long_days AS integer))
              LIMIT :limit)
        """,
    ),
    (
        "finished_jobs",
        """
        DELETE FROM gateway_jobs WHERE ctid IN (
            SELECT ctid FROM gateway_jobs
            WHERE status IN ('DONE', 'DEAD')
              AND finished_at < now() - make_interval(days => CAST(:job_days AS integer))
              LIMIT :limit)
        """,
    ),
    (
        "idempotency_keys",
        """
        DELETE FROM api_idempotency_keys WHERE ctid IN (
            SELECT ctid FROM api_idempotency_keys
            WHERE operation_status IN ('COMPLETED', 'FAILED') AND resolved_at IS NOT NULL
              AND resolved_at < now() - make_interval(days => CAST(:idem_days AS integer))
              LIMIT :limit)
        """,
    ),
    (
        "payment_attempts",
        """
        DELETE FROM payment_attempts WHERE ctid IN (
            SELECT ctid FROM payment_attempts
            WHERE started_at < now() - make_interval(days => CAST(:long_days AS integer))
            LIMIT :limit)
        """,
    ),
    (
        "webhook_deliveries",
        """
        DELETE FROM webhook_deliveries WHERE ctid IN (
            SELECT ctid FROM webhook_deliveries
            WHERE delivery_status IN ('DELIVERED', 'DEAD_LETTER')
              AND created_at < now() - make_interval(days => CAST(:long_days AS integer))
              LIMIT :limit)
        """,
    ),
    (
        "audit_logs",
        """
        DELETE FROM audit_logs WHERE ctid IN (
            SELECT ctid FROM audit_logs
            WHERE created_at < now() - make_interval(days => CAST(:long_days AS integer))
            LIMIT :limit)
        """,
    ),
]


async def handle_purge_retention(job: dict[str, Any]) -> None:
    """payment_transactions and payment_events are the financial record: never purged."""
    settings = get_settings()
    params = {
        "short_days": settings.RETENTION_WEBHOOK_EVENT_DAYS,
        "long_days": settings.RETENTION_LONG_DAYS,
        "job_days": settings.RETENTION_JOB_DAYS,
        "idem_days": settings.RETENTION_IDEMPOTENCY_DAYS,
        "limit": settings.PURGE_BATCH_LIMIT,
    }
    for name, sql in _PURGES:
        for _ in range(5):  # bounded passes per run; the rest goes tomorrow
            async with transaction() as conn:
                removed = await execute(conn, sql, params)
            if removed:
                log.info("retention_purge", table=name, rows=removed)
            if removed < settings.PURGE_BATCH_LIMIT:
                break
