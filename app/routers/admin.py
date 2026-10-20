"""Admin-token endpoints (TRD 5.2): reconciliation, delivery replay, event reprocess.

Every call writes an audit_logs row with actor_type = ADMIN.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from app.config import get_settings
from app.database import transaction
from app.dependencies import AdminContext, require_admin
from app.domain import JobType
from app.exceptions import GatewayError, not_found
from app.repositories import deliveries as deliveries_repo
from app.repositories import jobs as jobs_repo
from app.repositories import payments as payments_repo
from app.repositories.audit import write_audit
from app.services import qstash, reconciliation
from app.services.job_errors import DeadJob, RetryJob
from app.services.job_handlers import reprocess_event

router = APIRouter(prefix="/internal", tags=["admin"], include_in_schema=False)


def _uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise GatewayError(404, "NOT_FOUND", f"{what} not found.") from exc


@router.post("/reconciliation/{payment_id}")
async def reconcile(
    payment_id: str,
    request: Request,
    inline: bool = False,
    admin: AdminContext = Depends(require_admin),
) -> dict[str, Any]:
    pid = _uuid(payment_id, "Payment")
    settings = get_settings()
    async with transaction() as conn:
        payment = await payments_repo.get_by_id(conn, pid)
        if payment is None:
            raise not_found()
        await write_audit(
            conn, entity_type="PAYMENT", entity_id=str(pid), actor_type="ADMIN",
            actor_id=admin.operator, action="reconcile", payment_id=pid,
            metadata={"inline": inline},
        )  # fmt: skip
    if inline:
        result = await reconciliation.inquire_and_apply(pid, source="RECONCILE")
        return {"outcome": result.outcome.value, "status": result.status}

    async with transaction() as conn:
        job_id = await jobs_repo.enqueue(
            conn,
            JobType.RECONCILE_PAYMENT,
            ref_id=pid,
            dedupe_key=f"reconcile:{pid}:manual:{uuid.uuid4().hex[:8]}",
            max_attempts=settings.INQUIRY_MAX_ATTEMPTS,
        )
    return {"job_id": str(job_id), "status": "QUEUED"}


@router.post("/deliveries/{delivery_id}/replay")
async def replay_delivery(delivery_id: str, admin: AdminContext = Depends(require_admin)) -> Any:
    did = _uuid(delivery_id, "Delivery")
    settings = get_settings()
    async with transaction() as conn:
        delivery = await deliveries_repo.get_basic(conn, did)
        if delivery is None:
            raise GatewayError(404, "NOT_FOUND", "Delivery not found.")
        if delivery["delivery_status"] != "DEAD_LETTER":
            raise GatewayError(
                409, "DELIVERY_NOT_REPLAYABLE", "Only dead-lettered deliveries can be replayed."
            )
        key = f"deliver:{delivery['payment_event_id']}:{delivery['client_id']}"
        await deliveries_repo.set_status(conn, did, "PENDING")
        if not await jobs_repo.reset_job(conn, key):
            await jobs_repo.enqueue(
                conn,
                JobType.DELIVER_WEBHOOK,
                ref_id=did,
                dedupe_key=key,
                max_attempts=settings.DELIVERY_MAX_ATTEMPTS,
            )
        await write_audit(
            conn, entity_type="DELIVERY", entity_id=str(did), actor_type="ADMIN",
            actor_id=admin.operator, action="replay",
        )  # fmt: skip
    return JSONResponse(
        {"status": "QUEUED"}, status_code=202, background=BackgroundTask(qstash.nudge)
    )


@router.post("/webhook-events/{event_id}/reprocess")
async def reprocess(event_id: str, admin: AdminContext = Depends(require_admin)) -> dict[str, Any]:
    eid = _uuid(event_id, "Event")
    async with transaction() as conn:
        await write_audit(
            conn, entity_type="WEBHOOK_EVENT", entity_id=str(eid), actor_type="ADMIN",
            actor_id=admin.operator, action="reprocess",
        )  # fmt: skip
    try:
        outcome = await reprocess_event(eid)
    except RetryJob as exc:
        raise GatewayError(409, "REPROCESS_PENDING", f"Not resolvable yet: {exc.category}") from exc
    except DeadJob as exc:
        raise GatewayError(422, "REPROCESS_IMPOSSIBLE", exc.category) from exc
    return {"outcome": outcome.value}
