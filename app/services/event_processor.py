"""Applies a verified provider event inside ONE database transaction (TRD 11, 14).

inbox insert (dedupe) -> lock payment -> validate -> guarded transition ->
mark event processed -> payment_events row -> delivery + job. Commit, then ack.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.adapters.faspay.base import InquiryPolicy, VerifiedEvent
from app.adapters.faspay.status_map import map_legacy_status, map_snap_status
from app.config import get_settings
from app.database import transaction
from app.domain import JobType, PaymentStatus
from app.observability import alert, emit_metric
from app.repositories import events as events_repo
from app.repositories import jobs as jobs_repo
from app.repositories import payments as payments_repo
from app.services.state_machine import can_transition, is_contradiction
from app.services.transitions import apply_transition
from app.utils.hashing import sha256_hex
from app.utils.timeutil import utcnow

DEFERRAL_DELAY_SECONDS = 15


class ProcessOutcome(StrEnum):
    APPLIED = "APPLIED"
    NOOP = "NOOP"
    RECORDED = "RECORDED"  # stored, status unchanged (refund, unmapped value)
    DUPLICATE = "DUPLICATE"
    UNMATCHED = "UNMATCHED"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    CONTRADICTION = "CONTRADICTION"
    UNVERIFIED_AMOUNT = "UNVERIFIED_AMOUNT"


@dataclass
class ProcessResult:
    outcome: ProcessOutcome
    payment: dict[str, Any] | None = None
    event_id: uuid.UUID | None = None
    job_enqueued: bool = False
    reason: str | None = None


# ── Event (de)serialization for the inbox ──────────────────────────────


def verification_method(ev: VerifiedEvent) -> str:
    if ev.verification_method == "SNAP_SIGNATURE" and ev.inquiry_done:
        return "SNAP_SIGNATURE_AND_INQUIRY"
    return ev.verification_method


def storage_payload(ev: VerifiedEvent) -> dict[str, Any]:
    """Redacted raw payload plus the normalized fields needed to reprocess later."""
    return {
        "raw": ev.payload_redacted,
        "normalized": {
            "shape": ev.shape,
            "partner_reference": ev.partner_reference,
            "provider_reference": ev.provider_reference,
            "amount": ev.amount,
            "currency": ev.currency,
            "provider_status": ev.provider_status,
            "paid_at": ev.paid_at.isoformat() if ev.paid_at else None,
            "channel_code": ev.channel_code,
            "signature_valid": ev.signature_valid,
            "verification_method": ev.verification_method,
            "inquiry_policy": ev.inquiry_policy.value,
            "inquiry_done": ev.inquiry_done,
            "extras": ev.extras,
        },
    }


def event_from_row(row: dict[str, Any], merchant: dict[str, Any]) -> VerifiedEvent:
    stored = row["payload_redacted"] or {}
    n = stored.get("normalized")
    if not n:
        raise ValueError("event has no normalized payload and cannot be reprocessed")
    protocol = row["protocol"]
    mapping = (
        map_snap_status(n["provider_status"])
        if protocol == "SNAP"
        else map_legacy_status(n["provider_status"])
    )
    paid_at = datetime.fromisoformat(n["paid_at"]) if n.get("paid_at") else None
    return VerifiedEvent(
        protocol=protocol,
        source_path=row["source_path"],
        merchant=merchant,
        shape=n["shape"],
        partner_reference=n["partner_reference"],
        provider_reference=n.get("provider_reference"),
        amount=n.get("amount"),
        currency=n.get("currency"),
        provider_status=n["provider_status"],
        status=mapping.status,
        mapping=mapping,
        paid_at=paid_at,
        channel_code=n.get("channel_code"),
        fingerprint=row["event_fingerprint"],
        payload_redacted=stored.get("raw", {}),
        signature_valid=bool(n.get("signature_valid")),
        verification_method=n["verification_method"],
        inquiry_policy=InquiryPolicy(n["inquiry_policy"]),
        inquiry_done=bool(n.get("inquiry_done")),
        extras=n.get("extras") or {},
    )


# ── Rejected events (failed verification) ──────────────────────────────

_reject_window: list[float] = []


def _reject_allowed() -> bool:
    """Per-instance guard so a flood of forged calls cannot fill the database."""
    now = time.monotonic()
    limit = get_settings().REJECTED_EVENTS_PER_MINUTE
    while _reject_window and now - _reject_window[0] > 60:
        _reject_window.pop(0)
    if len(_reject_window) >= limit:
        return False
    _reject_window.append(now)
    return True


async def store_rejected(
    *,
    protocol: str,
    source_path: str,
    merchant_account_id: uuid.UUID | None,
    reason: str,
    payload: dict[str, Any] | None,
    provider_status: str | None = None,
    partner_reference: str | None = None,
) -> None:
    """A random salt keeps a forged payload from occupying a genuine event's fingerprint."""
    emit_metric("callback_rejected", protocol=protocol, reason=reason)
    if not _reject_allowed():
        return
    async with transaction() as conn:
        await events_repo.insert_inbox_event(
            conn,
            merchant_account_id=merchant_account_id,
            protocol=protocol,
            source_path=source_path,
            provider_event_key=partner_reference,
            fingerprint=sha256_hex(secrets.token_hex(16) + reason),
            event_type="payment.notification",
            provider_status_code=(provider_status or "")[:8] or None,
            processing_status="REJECTED",
            signature_valid=False,
            verification_method=reason[:32],
            payload={"raw": payload or {}, "reason": reason},
        )


# ── Core ───────────────────────────────────────────────────────────────


def _shape_compatible(ev: VerifiedEvent, payment: dict[str, Any]) -> bool:
    method: str = payment["payment_method"]
    if ev.shape == "VA":
        return method == "VA"
    if ev.shape == "REFERENCE":
        return method in ("QRIS", "EWALLET")
    return bool(payment["protocol"] == "LEGACY")


async def process_event(
    ev: VerifiedEvent, *, existing_event_id: uuid.UUID | None = None
) -> ProcessResult:
    async with transaction() as conn:
        event_id = existing_event_id
        if event_id is None:
            event_id = await events_repo.insert_inbox_event(
                conn,
                merchant_account_id=ev.merchant["id"],
                protocol=ev.protocol,
                source_path=ev.source_path,
                provider_event_key=ev.partner_reference,
                fingerprint=ev.fingerprint,
                event_type=ev.event_type,
                provider_status_code=ev.provider_status[:8] or None,
                processing_status="UNMATCHED",  # placeholder until the decision below
                signature_valid=ev.signature_valid,
                verification_method=verification_method(ev),
                payload=storage_payload(ev),
            )
            if event_id is None:
                existing = await events_repo.get_by_fingerprint(conn, ev.fingerprint)
                if existing is None or existing["processing_status"] == "PROCESSED":
                    return ProcessResult(ProcessOutcome.DUPLICATE, event_id=None)
                # A retry of an UNMATCHED / review event is re-correlated, not blindly acked.
                event_id = existing["id"]
        payment = await payments_repo.find_by_partner_reference(
            conn, ev.merchant["id"], ev.partner_reference, for_update=True
        )
        return await _decide(conn, ev, payment, event_id)


async def _decide(
    conn: AsyncConnection, ev: VerifiedEvent, payment: dict[str, Any] | None, event_id: uuid.UUID
) -> ProcessResult:
    settings = get_settings()
    if payment is None:
        return await _unmatched(
            conn, ev, event_id, "no_payment", settings.CORRELATION_RETRY_DELAY_SECONDS
        )
    if payment["protocol"] != ev.protocol or not _shape_compatible(ev, payment):
        alert("callback_shape_mismatch", protocol=ev.protocol, payment_id=str(payment["id"]))
        return await _unmatched(
            conn, ev, event_id, "incompatible", settings.CORRELATION_RETRY_DELAY_SECONDS
        )
    if (
        payment["provider_reference"]
        and ev.provider_reference
        and payment["provider_reference"] != ev.provider_reference
    ):
        alert("callback_reference_mismatch", payment_id=str(payment["id"]))
        return await _unmatched(
            conn, ev, event_id, "reference_mismatch", settings.CORRELATION_RETRY_DELAY_SECONDS
        )

    current = PaymentStatus(payment["status"])
    if current == PaymentStatus.CREATED:
        # The create response is not persisted yet: hold the event, retry shortly.
        return await _unmatched(conn, ev, event_id, "create_in_flight", DEFERRAL_DELAY_SECONDS)

    if (ev.currency and ev.currency != payment["currency"]) or (
        ev.amount is not None and ev.amount != payment["amount"]
    ):
        alert("callback_amount_mismatch", payment_id=str(payment["id"]), protocol=ev.protocol)
        await events_repo.set_event_result(
            conn, event_id, processing_status="FAILED", payment_id=payment["id"]
        )
        return ProcessResult(ProcessOutcome.AMOUNT_MISMATCH, payment, event_id, reason="amount")

    if ev.status == PaymentStatus.SUCCEEDED and ev.amount is None and _amount_unverifiable(ev):
        await events_repo.set_event_result(
            conn, event_id, processing_status="FAILED", payment_id=payment["id"]
        )
        queued = await jobs_repo.enqueue(
            conn,
            JobType.INQUIRE_PAYMENT,
            ref_id=payment["id"],
            dedupe_key=f"inquire:{payment['id']}:cb:{event_id}",
            max_attempts=settings.INQUIRY_MAX_ATTEMPTS,
        )
        alert("callback_amount_unverified", payment_id=str(payment["id"]))
        return ProcessResult(
            ProcessOutcome.UNVERIFIED_AMOUNT, payment, event_id, job_enqueued=queued is not None
        )

    # A lost create response is recovered here: fill the provider reference.
    if not payment["provider_reference"] and ev.provider_reference:
        await payments_repo.set_provider_fields(
            conn,
            payment["id"],
            provider_status=None,
            provider_reference=ev.provider_reference,
        )

    if ev.status is None:
        await payments_repo.set_provider_fields(
            conn, payment["id"], provider_status=ev.provider_status, provider_reference=None
        )
        if ev.mapping.alert:
            alert(ev.mapping.alert, payment_id=str(payment["id"]), raw_status=ev.provider_status)
        queued = None
        if ev.mapping.enqueue_inquiry:
            queued = await jobs_repo.enqueue(
                conn,
                JobType.INQUIRE_PAYMENT,
                ref_id=payment["id"],
                dedupe_key=f"inquire:{payment['id']}:cb:{event_id}",
                max_attempts=settings.INQUIRY_MAX_ATTEMPTS,
            )
        await _processed(conn, event_id, payment, ev)
        return ProcessResult(
            ProcessOutcome.RECORDED, payment, event_id, job_enqueued=queued is not None
        )

    target = ev.status
    if target == current:
        await payments_repo.set_provider_fields(
            conn, payment["id"], provider_status=ev.provider_status, provider_reference=None
        )
        await _processed(conn, event_id, payment, ev)
        return ProcessResult(ProcessOutcome.NOOP, payment, event_id)

    if is_contradiction(current, target):
        alert(
            "callback_contradicts_final_state",
            payment_id=str(payment["id"]),
            current=current.value,
            incoming=target.value,
        )
        await events_repo.set_event_result(
            conn, event_id, processing_status="FAILED", payment_id=payment["id"]
        )
        await jobs_repo.enqueue(
            conn,
            JobType.RECONCILE_PAYMENT,
            ref_id=payment["id"],
            dedupe_key=f"reconcile:{payment['id']}:contradiction",
            max_attempts=settings.INQUIRY_MAX_ATTEMPTS,
        )
        return ProcessResult(ProcessOutcome.CONTRADICTION, payment, event_id)

    if not can_transition(current, target):
        alert("callback_invalid_transition", payment_id=str(payment["id"]), to=target.value)
        await _processed(conn, event_id, payment, ev)
        return ProcessResult(
            ProcessOutcome.RECORDED, payment, event_id, reason="invalid_transition"
        )

    paid_at = (ev.paid_at or utcnow()) if target == PaymentStatus.SUCCEEDED else None
    transition = await apply_transition(
        conn,
        payment,
        target,
        source="CALLBACK",
        provider_status=ev.provider_status,
        paid_at=paid_at,
        webhook_event_id=event_id,
        occurred_at=ev.paid_at if target == PaymentStatus.SUCCEEDED else None,
    )
    await _processed(conn, event_id, payment, ev)
    emit_metric("callback_applied", protocol=ev.protocol, status=target.value)
    return ProcessResult(
        ProcessOutcome.APPLIED if transition.applied else ProcessOutcome.NOOP,
        payment,
        event_id,
        job_enqueued=transition.job_enqueued,
    )


def _amount_unverifiable(ev: VerifiedEvent) -> bool:
    if ev.protocol == "LEGACY":
        return get_settings().LEGACY_REQUIRE_AMOUNT_FOR_SUCCESS
    return not ev.inquiry_done  # SNAP: the inquiry (or the signed amount) must vouch for it


async def _processed(
    conn: AsyncConnection, event_id: uuid.UUID, payment: dict[str, Any], ev: VerifiedEvent
) -> None:
    await events_repo.set_event_result(
        conn,
        event_id,
        processing_status="PROCESSED",
        payment_id=payment["id"],
        verification_method=verification_method(ev),
    )


async def _unmatched(
    conn: AsyncConnection, ev: VerifiedEvent, event_id: uuid.UUID, reason: str, delay: float
) -> ProcessResult:
    settings = get_settings()
    await events_repo.set_event_result(conn, event_id, processing_status="UNMATCHED")
    await jobs_repo.enqueue(
        conn,
        JobType.RETRY_CORRELATION,
        ref_id=event_id,
        dedupe_key=f"recorr:{event_id}",
        delay_seconds=delay,
        max_attempts=settings.CORRELATION_MAX_ATTEMPTS,
    )
    emit_metric("callback_unmatched", protocol=ev.protocol, reason=reason)
    return ProcessResult(ProcessOutcome.UNMATCHED, None, event_id, reason=reason)
