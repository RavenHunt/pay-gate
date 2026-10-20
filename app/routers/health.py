from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.database import ping
from app.observability import get_logger

router = APIRouter(tags=["health"])
log = get_logger("health")


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up. No dependencies are touched."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness: the database answers."""
    try:
        await ping()
    except Exception as exc:  # noqa: BLE001
        log.warning("readiness_failed", error_type=type(exc).__name__)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ready"})
