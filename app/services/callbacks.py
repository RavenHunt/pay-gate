"""Callback finalization shared by the ingress services, jobs and admin reprocess.

Between signature verification and the database transaction sits the provider
inquiry (SIGNATURE_AND_INQUIRY, unsigned SNAP, and always for Legacy). It runs
outside any transaction, and only when the payment is known, so forged
notifications cannot make the gateway call Faspay for arbitrary references.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any

from app.adapters.faspay.base import (
    InquiryOutcome,
    InquiryPolicy,
    InquiryResult,
    InquiryTarget,
    VerifiedEvent,
)
from app.adapters.faspay.registry import get_driver
from app.config import ConfigError, get_settings
from app.database import transaction
from app.domain import JobType, PaymentStatus
from app.observability import alert, emit_metric
from app.repositories import events as events_repo
from app.repositories import jobs as jobs_repo
from app.repositories import payments as payments_repo
from app.services.event_processor import (
    ProcessOutcome,
    ProcessResult,
    process_event,
    store_rejected,
)


@dataclass
class FinalizeResult:
    process: ProcessResult | None = None
    try_later: str | None = None  # retryable: inquiry failed or disagreed
    inquiry: InquiryResult | None = None


def target_from(
    ev: VerifiedEvent, payment: dict[str, Any] | None, merchant: dict[str, Any]
) -> InquiryTarget:
    instruction = (payment or {}).get("instruction") or {}
    snap_extras = dict(instruction.get("_snap") or {})
    snap_extras.update({k: v for k, v in ev.extras.items() if v})
    method = (payment or {}).get("payment_method") or (
        "VA" if ev.shape == "VA" else "QRIS" if ev.shape == "REFERENCE" else "RETAIL"
    )
    return InquiryTarget(
        protocol=ev.protocol,
        payment_method=method,
        partner_reference=ev.partner_reference,
        provider_reference=(payment or {}).get("provider_reference") or ev.provider_reference,
        channel_code=ev.channel_code or (payment or {}).get("channel_code"),
        merchant=merchant,
        amount=(payment or {}).get("amount"),
        snap_extras=snap_extras,
    )


def _needs_inquiry(ev: VerifiedEvent, payment: dict[str, Any] | None) -> bool:
    if payment is None or ev.inquiry_done:
        return False
    if payment["status"] == PaymentStatus.CREATED:
        return False  # the event will be deferred anyway
    if ev.inquiry_policy == InquiryPolicy.ALWAYS:
        return True
    return (
        ev.inquiry_policy == InquiryPolicy.ON_SUCCESS
        and ev.status == PaymentStatus.SUCCEEDED
        and payment["status"] != PaymentStatus.SUCCEEDED
    )


async def finalize_event(
    ev: VerifiedEvent, *, existing_event_id: uuid.UUID | None = None
) -> FinalizeResult:
    async with transaction() as conn:
        if existing_event_id is None:
            existing = await events_repo.get_by_fingerprint(conn, ev.fingerprint)
            if existing and existing["processing_status"] == "PROCESSED":
                return FinalizeResult(process=ProcessResult(ProcessOutcome.DUPLICATE))
        payment = await payments_repo.find_by_partner_reference(
            conn, ev.merchant["id"], ev.partner_reference
        )

    inquiry: InquiryResult | None = None
    if _needs_inquiry(ev, payment):
        target = target_from(ev, payment, ev.merchant)
        try:
            inquiry = await get_driver(ev.protocol).inquire_payment(target)
        except ConfigError:
            alert("merchant_credentials_unavailable", merchant_code=ev.merchant["merchant_code"])
            return FinalizeResult(try_later="credentials_unavailable")
        emit_metric("callback_inquiry", protocol=ev.protocol, outcome=inquiry.outcome.value)
        if inquiry.outcome != InquiryOutcome.OK:
            return FinalizeResult(
                try_later=f"inquiry_{inquiry.outcome.value.lower()}", inquiry=inquiry
            )

        claimed_paid = ev.status == PaymentStatus.SUCCEEDED
        if claimed_paid and inquiry.status != PaymentStatus.SUCCEEDED:
            # The notification says paid but Faspay does not (yet). Do not acknowledge:
            # Faspay retries, and a follow-up inquiry converges even if it stops retrying.
            alert(
                "callback_inquiry_disagrees",
                protocol=ev.protocol,
                claimed=ev.provider_status,
                inquired=inquiry.provider_status,
            )
            await _schedule_claimed_paid_check(payment)
            return FinalizeResult(try_later="inquiry_disagrees", inquiry=inquiry)
        if ev.inquiry_policy == InquiryPolicy.ON_SUCCESS:
            ev = replace(ev, inquiry_done=True)
        else:  # ALWAYS: the inquiry result is the status
            ev = replace(
                ev,
                status=inquiry.status,
                provider_status=inquiry.provider_status or ev.provider_status,
                mapping=inquiry.mapping or ev.mapping,
                inquiry_done=True,
            )
        if inquiry.amount is not None:
            ev = replace(ev, amount=inquiry.amount)  # the inquiry is authoritative

    result = await process_event(ev, existing_event_id=existing_event_id)
    return FinalizeResult(process=result, inquiry=inquiry)


async def _schedule_claimed_paid_check(payment: dict[str, Any] | None) -> None:
    if payment is None:
        return
    settings = get_settings()
    async with transaction() as conn:
        await jobs_repo.enqueue(
            conn,
            JobType.INQUIRE_PAYMENT,
            ref_id=payment["id"],
            dedupe_key=f"inquire:{payment['id']}:claimed-paid",
            delay_seconds=settings.UNKNOWN_INQUIRY_DELAY_SECONDS,
            max_attempts=settings.INQUIRY_MAX_ATTEMPTS,
            args={"expect": PaymentStatus.SUCCEEDED.value},
        )


async def reject_unverifiable(ev: VerifiedEvent, reason: str) -> None:
    await store_rejected(
        protocol=ev.protocol,
        source_path=ev.source_path,
        merchant_account_id=ev.merchant["id"],
        reason=reason,
        payload=ev.payload_redacted,
        provider_status=ev.provider_status,
        partner_reference=ev.partner_reference,
    )
