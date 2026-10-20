"""Legacy notification ingress (TRD 9.3). The inquiry is the authoritative control."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse

from app.adapters.faspay.legacy.verify import (
    LegacyFormatError,
    LegacySignatureError,
    ParsedLegacy,
    parse_notification,
    verify_callback,
)
from app.config import get_settings
from app.observability import get_logger
from app.services.callbacks import finalize_event, reject_unverifiable
from app.services.event_processor import ProcessOutcome, store_rejected
from app.services.merchant_lookup import merchant_by_provider_id
from app.utils.timeutil import legacy_datetime, utcnow

log = get_logger("ingress.legacy")
SOURCE_PATH = "/v1/webhooks/faspay/legacy/notification"


@dataclass
class IngressResult:
    response: JSONResponse
    nudge: bool = False


def _reply(
    parsed: ParsedLegacy | None,
    merchant: dict[str, Any] | None,
    code: str,
    desc: str,
    http_status: int = 200,
) -> JSONResponse:
    body = {
        "response": "Payment Notification",
        "trx_id": parsed.trx_id if parsed else "",
        "merchant_id": parsed.merchant_id if parsed else "",
        "merchant": (merchant or {}).get("merchant_name") or "",
        "bill_no": parsed.bill_no if parsed else "",
        "response_code": code,
        "response_desc": desc,
        "response_date": legacy_datetime(utcnow()),
    }
    return JSONResponse(body, status_code=http_status)


async def handle_legacy_notification(*, raw_body: bytes) -> IngressResult:
    try:
        return await _handle(raw_body)
    except Exception as exc:  # noqa: BLE001
        log.error("legacy_ingress_error", error_type=type(exc).__name__, exc_info=exc)
        return IngressResult(
            _reply(None, None, get_settings().LEGACY_ERROR_RESPONSE_CODE, "Internal error", 500)
        )


async def _handle(raw_body: bytes) -> IngressResult:
    settings = get_settings()
    error_code = settings.LEGACY_ERROR_RESPONSE_CODE
    try:
        parsed = parse_notification(json.loads(raw_body))
    except (ValueError, LegacyFormatError) as exc:
        await store_rejected(
            protocol="LEGACY", source_path=SOURCE_PATH, merchant_account_id=None,
            reason="malformed", payload=None,
        )  # fmt: skip
        return IngressResult(_reply(None, None, error_code, f"Invalid request: {exc}", 400))

    merchant = await merchant_by_provider_id(parsed.merchant_id)
    if merchant is None or merchant["status"] == "INACTIVE":
        await store_rejected(
            protocol="LEGACY", source_path=SOURCE_PATH, merchant_account_id=None,
            reason="unknown_merchant", payload=None, partner_reference=parsed.bill_no,
        )  # fmt: skip
        return IngressResult(_reply(parsed, None, error_code, "Merchant not found"))

    try:
        event = verify_callback(parsed, merchant=merchant, source_path=SOURCE_PATH)
    except LegacySignatureError as exc:
        await store_rejected(
            protocol="LEGACY", source_path=SOURCE_PATH, merchant_account_id=merchant["id"],
            reason=str(exc), payload=None, provider_status=parsed.status_code,
            partner_reference=parsed.bill_no,
        )  # fmt: skip
        return IngressResult(_reply(parsed, merchant, error_code, "Invalid signature", 401))

    final = await finalize_event(event)
    if final.try_later:
        await reject_unverifiable(event, final.try_later)
        # Non-success so that Faspay retries (the existing app would echo the inquiry error).
        return IngressResult(_reply(parsed, merchant, error_code, "Unable to confirm payment"))

    result = final.process
    assert result is not None
    outcome = result.outcome
    if outcome in (
        ProcessOutcome.UNMATCHED,
        ProcessOutcome.AMOUNT_MISMATCH,
    ):
        desc = "Amount mismatch" if outcome == ProcessOutcome.AMOUNT_MISMATCH else "Bill not found"
        return IngressResult(_reply(parsed, merchant, error_code, desc))

    # Success reply: the existing app copies the inquiry's response code and description.
    code, desc = "00", "Success"
    if final.inquiry and settings.LEGACY_ACK_USE_INQUIRY_CODE and final.inquiry.response_code:
        code = final.inquiry.response_code
    return IngressResult(_reply(parsed, merchant, code, desc), nudge=result.job_enqueued)
