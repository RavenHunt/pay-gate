"""Legacy Post Data Transaction and Inquiry mapping (TRD 9.3)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.adapters.faspay.base import CreateContext, InquiryTarget
from app.adapters.faspay.credentials import LegacyCredentials
from app.adapters.faspay.legacy.signing import legacy_signature
from app.adapters.faspay.snap.mappers import deep_merge
from app.config import get_settings
from app.utils.timeutil import iso_jakarta, legacy_datetime

CREATE_PATH = "/cvr/300011/10"
INQUIRY_PATH = "/cvr/100004/10"


def amount_to_legacy(amount: int) -> str:
    """Rupiah x 100 (the sample `1000` means Rp 10)."""
    return str(amount * 100)


def legacy_to_rupiah(value: Any) -> int:
    try:
        dec = Decimal(str(value).strip()) / 100
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount is not numeric") from exc
    if not dec.is_finite() or dec < 0 or dec != dec.to_integral_value():
        raise ValueError("amount must be a whole rupiah value")
    return int(dec)


def build_create_body(ctx: CreateContext, creds: LegacyCredentials) -> dict[str, Any]:
    settings = get_settings()
    merchant = ctx.merchant
    total = amount_to_legacy(ctx.amount)
    description = (ctx.description or f"Payment {ctx.partner_reference}")[:100]
    body: dict[str, Any] = {
        "request": "Post Data Transaction",
        "merchant_id": merchant.get("provider_merchant_id"),
        "merchant": merchant.get("merchant_name") or "",
        "bill_no": ctx.partner_reference,
        "bill_reff": ctx.partner_reference,  # never the consumer's reference
        "bill_date": legacy_datetime(ctx.created_at),
        "bill_expired": legacy_datetime(ctx.expires_at),
        "bill_desc": description,
        "bill_currency": "IDR",
        "bill_gross": "0",
        "bill_miscfee": "0",
        "bill_total": total,
        "cust_no": ctx.partner_reference,
        "cust_name": ctx.customer.name or settings.DEFAULT_CUSTOMER_NAME,
        "msisdn": ctx.customer.phone or "",
        "email": ctx.customer.email or "",
        "payment_channel": ctx.channel_code,
        "pay_type": settings.LEGACY_PAY_TYPE,
        "terminal": settings.LEGACY_TERMINAL,
        "item": [
            {
                "product": description,
                "qty": "1",
                "amount": total,
                "payment_plan": "01",
                "merchant_id": merchant.get("provider_merchant_id"),
                "tenor": "00",
            }
        ],
        "signature": legacy_signature(creds.user_id, creds.password, ctx.partner_reference),
    }
    if settings.LEGACY_BODY_OVERRIDES:
        body = deep_merge(body, settings.LEGACY_BODY_OVERRIDES)
    return body


def build_inquiry_body(target: InquiryTarget, creds: LegacyCredentials) -> dict[str, Any]:
    return {
        "request": "Inquiry Status Payment",
        "trx_id": target.provider_reference,
        "merchant_id": target.merchant.get("provider_merchant_id"),
        "bill_no": target.partner_reference,
        "signature": legacy_signature(creds.user_id, creds.password, target.partner_reference),
    }


def parse_create_response(
    ctx: CreateContext, data: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    trx_id = data.get("trx_id")
    trx_id = str(trx_id).strip() if trx_id else None
    instruction = {
        "type": ctx.payment_method,
        "virtual_account_no": None,
        "payment_code": trx_id,
        "qr_url": None,
        "redirect_url": data.get("redirect_url") or None,
        "expires_at": iso_jakarta(ctx.expires_at),
    }
    return trx_id, instruction
