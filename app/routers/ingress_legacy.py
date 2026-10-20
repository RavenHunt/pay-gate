"""Legacy notification ingress."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from starlette.background import BackgroundTask

from app.services.legacy_ingress import handle_legacy_notification
from app.services.qstash import nudge

router = APIRouter(tags=["ingress"])


@router.post("/v1/webhooks/faspay/legacy/notification", include_in_schema=False)
async def legacy_notification(request: Request) -> Response:
    result = await handle_legacy_notification(raw_body=await request.body())
    if result.nudge:
        result.response.background = BackgroundTask(nudge)
    return result.response
