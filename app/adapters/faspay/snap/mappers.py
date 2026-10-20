"""Request/response mapping for SNAP VA, QRIS and e-wallet (TRD 9.2).

Field names follow the TRD and the existing integration. Faspay-specific
details that are still unconfirmed can be adjusted without code through
`SNAP_BODY_OVERRIDES` (deep-merged per payment method).
"""

from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.adapters.faspay.base import CreateContext, InquiryTarget
from app.config import get_settings
from app.utils.timeutil import iso_jakarta, to_jakarta

CREATE_PATHS = {
    "VA": "/v1.0/transfer-va/create-va",
    "QRIS": "/v1.0/qr/qr-mpm-generate",
    "EWALLET": "/v1.0/debit/payment-host-to-host",
}
INQUIRY_PATHS = {
    "VA": "/v1.0/transfer-va/status",
    "QRIS": "/v1.0/qr/qr-mpm-query",
    "EWALLET": "/v1.0/debit/status",
}


def amount_to_snap(amount: int) -> str:
    """Integer rupiah to the SNAP string with two decimals."""
    return f"{amount}.00"


def snap_to_rupiah(value: Any) -> int:
    """Parse a SNAP amount string into integer rupiah; reject sub-rupiah and negatives."""
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount is not numeric") from exc
    if not dec.is_finite() or dec < 0 or dec != dec.to_integral_value():
        raise ValueError("amount must be a whole non-negative rupiah value")
    return int(dec)


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _apply_overrides(method: str, body: dict[str, Any]) -> dict[str, Any]:
    extra = get_settings().SNAP_BODY_OVERRIDES.get(method)
    return deep_merge(body, extra) if extra else body


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def build_create_body(ctx: CreateContext) -> dict[str, Any]:
    merchant = ctx.merchant
    method = ctx.payment_method
    customer_name = ctx.customer.name or get_settings().DEFAULT_CUSTOMER_NAME
    amount = {"value": amount_to_snap(ctx.amount), "currency": "IDR"}
    validity = iso_jakarta(ctx.expires_at)
    description = ctx.description or f"Payment {ctx.partner_reference}"

    body: dict[str, Any]
    if method == "VA":
        # Faspay assigns the VA number: the request carries none (confirmed).
        body = {
            "trxId": ctx.partner_reference,
            "virtualAccountName": customer_name,
            "virtualAccountEmail": ctx.customer.email,
            "virtualAccountPhone": ctx.customer.phone,
            "totalAmount": amount,
            "expiredDate": validity,
            "additionalInfo": {
                "channelCode": ctx.channel_code,
                "merchantId": merchant.get("provider_merchant_id"),
                "billDescription": description[:100],
            },
        }
    elif method == "QRIS":
        body = {
            "partnerReferenceNo": ctx.partner_reference,
            "amount": amount,
            "merchantId": merchant.get("provider_merchant_id"),
            "validityPeriod": validity,
            "additionalInfo": {
                "billDate": to_jakarta(ctx.created_at).strftime("%Y-%m-%d %H:%M:%S"),
                "billDescription": description[:100],
                "channelCode": ctx.channel_code,
                "phoneNo": ctx.customer.phone,
            },
        }
    elif method == "EWALLET":
        body = {
            "partnerReferenceNo": ctx.partner_reference,
            "merchantId": merchant.get("provider_merchant_id"),
            "amount": amount,
            "validityPeriod": validity,
            "additionalInfo": {
                "channelCode": ctx.channel_code,
                "customerName": customer_name,
                "phoneNo": ctx.customer.phone,
                "email": ctx.customer.email,
                "orderDescription": description[:100],
            },
        }
    else:  # pragma: no cover - guarded by the routing layer
        raise ValueError(f"SNAP does not serve {method}")

    body = _drop_none(body)
    if isinstance(body.get("additionalInfo"), dict):
        body["additionalInfo"] = _drop_none(body["additionalInfo"])
    return _apply_overrides(method, body)


def build_inquiry_body(target: InquiryTarget) -> dict[str, Any] | None:
    """None when the inquiry cannot be formed (a VA whose number is unknown)."""
    merchant = target.merchant
    settings = get_settings()
    method = target.payment_method
    if method == "VA":
        if not target.provider_reference:
            return None
        extras = target.snap_extras
        va_no = str(extras.get("virtualAccountNo") or target.provider_reference)
        body = {
            "partnerServiceId": extras.get("partnerServiceId"),
            "customerNo": extras.get("customerNo"),
            "virtualAccountNo": va_no,
            "additionalInfo": {
                "channelCode": target.channel_code,
                "trxId": target.partner_reference,
            },
        }
    elif method == "QRIS":
        body = {
            "originalReferenceNo": target.provider_reference,
            "originalPartnerReferenceNo": target.partner_reference,
            "serviceCode": settings.SNAP_QRIS_QUERY_SERVICE_CODE,
            "merchantId": merchant.get("provider_merchant_id"),
            "additionalInfo": {"channelCode": target.channel_code},
        }
    elif method == "EWALLET":
        body = {
            "originalReferenceNo": target.provider_reference,
            "originalPartnerReferenceNo": target.partner_reference,
            "serviceCode": settings.SNAP_EWALLET_STATUS_SERVICE_CODE,
            "merchantId": merchant.get("provider_merchant_id"),
            "additionalInfo": {"channelCode": target.channel_code},
        }
    else:  # pragma: no cover
        return None
    body = _drop_none(body)
    if isinstance(body.get("additionalInfo"), dict):
        body["additionalInfo"] = _drop_none(body["additionalInfo"])
    overrides = settings.SNAP_BODY_OVERRIDES.get(f"{method}_INQUIRY")
    return deep_merge(body, overrides) if overrides else body


def _g(data: dict[str, Any] | None, *path: str) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def parse_create_response(
    method: str, body: dict[str, Any], expires_at: datetime
) -> tuple[str | None, dict[str, Any]]:
    """Returns (provider_reference, instruction). Reference None means malformed success."""
    expiry = iso_jakarta(expires_at)
    instruction: dict[str, Any] = {
        "type": method,
        "virtual_account_no": None,
        "payment_code": None,
        "qr_url": None,
        "redirect_url": None,
        "expires_at": expiry,
    }
    if method == "VA":
        data = body.get("virtualAccountData") or {}
        va_no = str(data.get("virtualAccountNo") or "").strip()
        instruction["virtual_account_no"] = va_no or None
        instruction["redirect_url"] = _g(data, "additionalInfo", "redirectUrl") or _g(
            body, "additionalInfo", "redirectUrl"
        )
        if data.get("expiredDate"):
            instruction["expires_at"] = str(data["expiredDate"])
        # Needed later to build the status inquiry; stripped from client responses.
        instruction["_snap"] = {
            "partnerServiceId": data.get("partnerServiceId"),
            "customerNo": data.get("customerNo"),
        }
        return (va_no or None), instruction
    reference = body.get("referenceNo")
    reference = str(reference).strip() if reference else None
    if method == "QRIS":
        instruction["qr_url"] = _g(body, "additionalInfo", "qrImageUrl") or body.get("qrUrl")
        instruction["redirect_url"] = _g(body, "additionalInfo", "redirectUrl")
    else:
        instruction["redirect_url"] = body.get("webRedirectUrl") or _g(
            body, "additionalInfo", "webRedirectUrl"
        )
    return reference, instruction


def extract_status(method: str, body: dict[str, Any]) -> Any:
    """Raw status from an inquiry response, tolerant to the documented placements."""
    candidates = (
        ("virtualAccountData", "paymentFlagStatus"),
        ("virtualAccountData", "additionalInfo", "latestTransactionStatus"),
        ("additionalInfo", "latestTransactionStatus"),
        ("latestTransactionStatus",),
        ("paymentFlagStatus",),
    )
    for path in candidates:
        value = _g(body, *path)
        if value not in (None, ""):
            return value
    return None


def extract_amount(body: dict[str, Any]) -> int | None:
    for path in (
        ("virtualAccountData", "paidAmount", "value"),
        ("virtualAccountData", "totalAmount", "value"),
        ("paidAmount", "value"),
        ("amount", "value"),
        ("transAmount", "value"),
    ):
        value = _g(body, *path)
        if value not in (None, ""):
            try:
                return snap_to_rupiah(value)
            except ValueError:
                return None
    return None
