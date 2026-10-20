"""Guarded status transitions, payment_events, and delivery/job creation.

Callers hold the payment row lock (SELECT ... FOR UPDATE); the conditional
UPDATE is a second guard, so a lost race returns `applied = False`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import get_settings
from app.domain import DELIVERED_EVENT_TYPES, EVENT_TYPES, FINAL_STATUSES, JobType, PaymentStatus
from app.observability import alert, emit_metric
from app.repositories import clients as clients_repo
from app.repositories import deliveries as deliveries_repo
from app.repositories import events as events_repo
from app.repositories import idempotency as idempotency_repo
from app.repositories import jobs as jobs_repo
from app.repositories import payments as payments_repo
from app.services.state_machine import can_transition


@dataclass
class TransitionResult:
    applied: bool
    payment_event_id: Any = None
    delivery_id: Any = None
    job_enqueued: bool = False


async def apply_transition(
    conn: AsyncConnection,
    payment: dict[str, Any],
    to_status: PaymentStatus,
    *,
    source: str,
    provider_status: str | None = None,
    paid_at: datetime | None = None,
    webhook_event_id: Any = None,
    occurred_at: datetime | None = None,
) -> TransitionResult:
    current = PaymentStatus(payment["status"])
    if not can_transition(current, to_status):
        return TransitionResult(applied=False)

    changed = await payments_repo.guarded_status_update(
        conn,
        payment["id"],
        from_status=current.value,
        to_status=to_status.value,
        provider_status=provider_status,
        paid_at=paid_at,
    )
    if not changed:
        return TransitionResult(applied=False)

    event_type = EVENT_TYPES[to_status]
    event_id = await events_repo.insert_payment_event(
        conn,
        payment_id=payment["id"],
        webhook_event_id=webhook_event_id,
        event_type=event_type,
        status_from=current.value,
        status_to=to_status.value,
        source=source,
        occurred_at=occurred_at,
    )
    emit_metric(
        "payment_transition",
        protocol=payment["protocol"],
        merchant_account_id=str(payment["merchant_account_id"]),
        status=to_status.value,
        source=source,
    )

    # The consumer already learned a synchronous create outcome from the API response.
    result = TransitionResult(applied=True, payment_event_id=event_id)
    if event_type in DELIVERED_EVENT_TYPES and source != "CREATE":
        await _enqueue_delivery(conn, payment, event_id, result)

    if current == PaymentStatus.UNKNOWN or to_status in FINAL_STATUSES:
        await idempotency_repo.resolve_for_payment(
            conn,
            payment["id"],
            operation_status="FAILED" if to_status == PaymentStatus.FAILED else "COMPLETED",
        )
    return result


async def _enqueue_delivery(
    conn: AsyncConnection, payment: dict[str, Any], event_id: Any, result: TransitionResult
) -> None:
    client = await clients_repo.get_client(conn, payment["client_id"])
    if not client or not client["webhook_url"]:
        alert("client_without_webhook", client_id=str(payment["client_id"]))
        return
    delivery_id = await deliveries_repo.insert_delivery(
        conn,
        payment_event_id=event_id,
        client_id=payment["client_id"],
        destination_url=client["webhook_url"],
    )
    if delivery_id is None:
        return
    await jobs_repo.enqueue(
        conn,
        JobType.DELIVER_WEBHOOK,
        ref_id=delivery_id,
        dedupe_key=f"deliver:{event_id}:{payment['client_id']}",
        max_attempts=get_settings().DELIVERY_MAX_ATTEMPTS,
    )
    result.delivery_id = delivery_id
    result.job_enqueued = True
