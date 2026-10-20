"""SNAP notification ingress: Faspay appends these paths to the registered merchant URL.

All three routes use one handler; the parser is chosen by payload shape, never
by route (the e-wallet notification may arrive on the VA path).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from starlette.background import BackgroundTask

from app.services.qstash import nudge
from app.services.snap_ingress import handle_snap_notification

router = APIRouter(tags=["ingress"])


async def _handle(request: Request) -> Response:
    result = await handle_snap_notification(
        raw_body=await request.body(),
        headers=request.headers,
        path=request.url.path,
        query=request.url.query,
        url=str(request.url),
    )
    if result.nudge:
        result.response.background = BackgroundTask(nudge)
    return result.response


@router.post("/v1.0/transfer-va/payment", include_in_schema=False)
async def snap_va_payment(request: Request) -> Response:
    return await _handle(request)


@router.post("/v1.0/qr/qr-mpm-notify", include_in_schema=False)
async def snap_qris_notify(request: Request) -> Response:
    return await _handle(request)


@router.post("/v1.0/debit/notify", include_in_schema=False)
async def snap_debit_notify(request: Request) -> Response:
    return await _handle(request)
