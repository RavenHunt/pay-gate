"""Request/response contract (TRD 6)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, field_validator

from app.config import get_settings
from app.domain import PaymentMethod
from app.utils.timeutil import iso_jakarta

_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#\-]{0,127}$")
_CHANNEL = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
_ROUTE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}$")
_PHONE = re.compile(r"^\+?[0-9]{6,20}$")


class CustomerIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is not None and not _EMAIL.match(value):
            raise ValueError("invalid email address")
        return value

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        if value is not None and not _PHONE.match(value):
            raise ValueError("invalid phone number")
        return value


class CreatePaymentRequest(BaseModel):
    # Extra fields (a client id, an app code, a callback URL) are ignored by design.
    model_config = ConfigDict(extra="ignore")

    merchant_reference: str
    amount: StrictInt
    currency: Literal["IDR"] = "IDR"
    payment_method: PaymentMethod
    channel_code: str
    route_code: str | None = None
    description: str | None = Field(default=None, max_length=255)
    customer: CustomerIn | None = None
    expires_at: AwareDatetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("merchant_reference")
    @classmethod
    def _reference(cls, value: str) -> str:
        if not _REFERENCE.match(value):
            raise ValueError("merchant_reference must be 1-128 safe characters")
        return value

    @field_validator("channel_code")
    @classmethod
    def _channel(cls, value: str) -> str:
        if not _CHANNEL.match(value):
            raise ValueError("invalid channel_code")
        return value

    @field_validator("route_code")
    @classmethod
    def _route(cls, value: str | None) -> str | None:
        if value is not None and not _ROUTE.match(value):
            raise ValueError("invalid route_code")
        return value

    @field_validator("amount")
    @classmethod
    def _amount(cls, value: int) -> int:
        s = get_settings()
        if value < s.PAYMENT_MIN_AMOUNT or value > s.PAYMENT_MAX_AMOUNT:
            raise ValueError(
                f"amount must be between {s.PAYMENT_MIN_AMOUNT} and {s.PAYMENT_MAX_AMOUNT}"
            )
        return value

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, str]) -> dict[str, str]:
        s = get_settings()
        if len(value) > s.METADATA_MAX_KEYS:
            raise ValueError(f"metadata allows at most {s.METADATA_MAX_KEYS} keys")
        for key, item in value.items():
            if not key or len(key) > s.METADATA_MAX_KEY_LENGTH:
                raise ValueError("metadata key length is out of range")
            if len(item) > s.METADATA_MAX_VALUE_LENGTH:
                raise ValueError("metadata value is too long")
        return value


def payment_view(row: dict[str, Any]) -> dict[str, Any]:
    """Normalized payment representation. Internal provider hints never leak."""
    stored = row.get("instruction") or {}
    instruction = {
        "type": stored.get("type") or row["payment_method"],
        "virtual_account_no": stored.get("virtual_account_no"),
        "payment_code": stored.get("payment_code"),
        "qr_url": stored.get("qr_url"),
        "redirect_url": stored.get("redirect_url"),
        "expires_at": stored.get("expires_at") or iso_jakarta(row.get("expires_at")),
    }
    return {
        "payment_id": str(row["id"]),
        "merchant_reference": row["merchant_reference"],
        "status": row["status"],
        "amount": row["amount"],
        "currency": row["currency"],
        "payment_method": row["payment_method"],
        "channel_code": row["channel_code"],
        "provider_reference": row.get("provider_reference"),
        "payment_instruction": instruction,
        "metadata": row.get("metadata") or {},
        "created_at": iso_jakarta(row["created_at"]),
        "paid_at": iso_jakarta(row.get("paid_at")),
        "expires_at": iso_jakarta(row.get("expires_at")),
    }


def event_payload(
    *, event_id: str, event_type: str, ctx: dict[str, Any], occurred_at: datetime
) -> dict[str, Any]:
    """Consumer webhook body, identical for every platform (TRD 6.6)."""
    status = ctx["status_to"]
    return {
        "event_id": event_id,
        "event_type": event_type,
        "payment": {
            "payment_id": str(ctx["payment_id"]),
            "merchant_reference": ctx["merchant_reference"],
            "provider_reference": ctx.get("provider_reference"),
            "status": status,
            "amount": ctx["amount"],
            "currency": ctx["currency"],
            "payment_method": ctx["payment_method"],
            "channel_code": ctx["channel_code"],
            "paid_at": iso_jakarta(ctx.get("paid_at")) if status == "SUCCEEDED" else None,
            "metadata": ctx.get("metadata") or {},
        },
        "occurred_at": iso_jakarta(occurred_at),
    }
