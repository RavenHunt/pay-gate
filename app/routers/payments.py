"""Client API: create and read payments (TRD 6)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from app.dependencies import AuthContext, require_scope
from app.domain import SCOPE_PAYMENTS_READ, SCOPE_PAYMENTS_WRITE
from app.exceptions import GatewayError
from app.schemas.payments import CreatePaymentRequest
from app.services import payment_service

router = APIRouter(prefix="/v1/payments", tags=["payments"])

_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")


def _idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise GatewayError(
            400, "IDEMPOTENCY_KEY_REQUIRED", "The Idempotency-Key header is required."
        )
    value = value.strip()
    if not _IDEMPOTENCY_KEY.match(value):
        raise GatewayError(
            400,
            "IDEMPOTENCY_KEY_INVALID",
            "Idempotency-Key must be 1-128 printable ASCII characters.",
        )
    return value


@router.post("")
async def create_payment(
    body: CreatePaymentRequest,
    auth: AuthContext = Depends(require_scope(SCOPE_PAYMENTS_WRITE)),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    key = _idempotency_key(idempotency_key)
    result = await payment_service.create_payment(auth, body, key)
    return JSONResponse(result.body, status_code=result.http_status)


@router.get("/by-reference/{merchant_reference:path}")
async def get_by_reference(
    merchant_reference: str,
    auth: AuthContext = Depends(require_scope(SCOPE_PAYMENTS_READ)),
) -> dict[str, Any]:
    return await payment_service.get_payment_by_reference(auth, merchant_reference)


@router.get("/{payment_id}")
async def get_payment(
    payment_id: str,
    auth: AuthContext = Depends(require_scope(SCOPE_PAYMENTS_READ)),
) -> dict[str, Any]:
    return await payment_service.get_payment(auth, payment_id)


@router.post("/{payment_id}/cancel")
async def cancel_payment(
    payment_id: str,
    auth: AuthContext = Depends(require_scope(SCOPE_PAYMENTS_WRITE)),
) -> JSONResponse:
    """Post-MVP: not supported by any channel yet."""
    raise GatewayError(501, "CANCEL_NOT_SUPPORTED", "Cancellation is not supported yet.")
