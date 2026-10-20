"""Handler registry for gateway_jobs. Each handler must be idempotent (leases can expire)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.database import transaction
from app.domain import JobType
from app.repositories import events as events_repo
from app.repositories import merchants as merchants_repo
from app.services import delivery, reconciliation
from app.services.callbacks import finalize_event
from app.services.event_processor import ProcessOutcome, event_from_row
from app.services.job_errors import DeadJob, RetryJob

Handler = Callable[[dict[str, Any]], Awaitable[None]]
DeadHook = Callable[[dict[str, Any], str], Awaitable[None]]


async def reprocess_event(event_id: uuid.UUID) -> ProcessOutcome:
    """Re-run correlation for a stored event. Raises RetryJob when it cannot finish yet."""
    async with transaction() as conn:
        row = await events_repo.get_event(conn, event_id)
        if row is None:
            raise DeadJob("event_missing")
        if row["processing_status"] == "PROCESSED":
            return ProcessOutcome.DUPLICATE
        if row["processing_status"] == "REJECTED" or row["merchant_account_id"] is None:
            raise DeadJob("event_not_reprocessable")
        merchant = await merchants_repo.get_merchant(conn, row["merchant_account_id"])
    if merchant is None:
        raise DeadJob("merchant_missing")
    try:
        ev = event_from_row(row, merchant)
    except (ValueError, KeyError) as exc:
        raise DeadJob("event_not_reprocessable") from exc

    final = await finalize_event(ev, existing_event_id=event_id)
    if final.try_later:
        raise RetryJob(final.try_later)
    assert final.process is not None
    return final.process.outcome


async def handle_retry_correlation(job: dict[str, Any]) -> None:
    outcome = await reprocess_event(job["ref_id"])
    if outcome == ProcessOutcome.UNMATCHED:
        raise RetryJob("still_unmatched")


def get_handlers() -> dict[str, Handler]:
    return {
        JobType.DELIVER_WEBHOOK: delivery.handle_deliver_webhook,
        JobType.RETRY_CORRELATION: handle_retry_correlation,
        JobType.INQUIRE_PAYMENT: reconciliation.handle_inquire_payment,
        JobType.RECONCILE_PAYMENT: reconciliation.handle_reconcile_payment,
        JobType.EXPIRE_PAYMENT: reconciliation.handle_expire_payment,
        JobType.PURGE_RETENTION: reconciliation.handle_purge_retention,
    }


def get_dead_hooks() -> dict[str, DeadHook]:
    return {JobType.DELIVER_WEBHOOK: delivery.on_delivery_dead}
