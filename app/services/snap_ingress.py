"""SNAP notification ingress (TRD 9.2, 11). All three routes share this handler."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from starlette.responses import JSONResponse

from app.adapters.faspay.snap import ack
from app.adapters.faspay.snap.verify import (
    NotificationFormatError,
    ParsedNotification,
    SignatureError,
    parse_notification,
    verify_callback,
)
from app.config import get_settings
from app.observability import get_logger
from app.services.callbacks import finalize_event, reject_unverifiable
from app.services.event_processor import ProcessOutcome, store_rejected
from app.services.merchant_lookup import merchant_by_provider_id

log = get_logger("ingress.snap")


@dataclass
class IngressResult:
    response: JSONResponse
    nudge: bool = False


def _service_for(path: str, parsed: ParsedNotification | None) -> str:
    route_service = ack.ROUTE_SERVICE.get(path, ack.SERVICE_VA)
    if parsed is None:
        return route_service
    if parsed.shape == "VA":
        return ack.SERVICE_VA
    # Reference shape is QRIS or e-wallet; the payment method refines it later.
    return route_service if route_service != ack.SERVICE_VA else ack.SERVICE_EWALLET


async def handle_snap_notification(
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    path: str,
    query: str,
    url: str,
) -> IngressResult:
    service = ack.ROUTE_SERVICE.get(path, ack.SERVICE_VA)
    try:
        return await _handle(raw_body, headers, path, query, url, service)
    except Exception as exc:  # noqa: BLE001 - a 5xx lets Faspay retry
        log.error("snap_ingress_error", error_type=type(exc).__name__, exc_info=exc)
        return IngressResult(ack.gateway_error(service))


async def _handle(
    raw_body: bytes,
    headers: Mapping[str, str],
    path: str,
    query: str,
    url: str,
    service: str,
) -> IngressResult:
    settings = get_settings()
    try:
        payload = json.loads(raw_body)
    except ValueError:
        await store_rejected(
            protocol="SNAP", source_path=path, merchant_account_id=None,
            reason="malformed_json", payload=None,
        )  # fmt: skip
        return IngressResult(ack.bad_format(service, "01", "Invalid JSON body"))

    try:
        parsed = parse_notification(payload)
    except NotificationFormatError as exc:
        await store_rejected(
            protocol="SNAP", source_path=path, merchant_account_id=None,
            reason=f"format_{exc.case}", payload=None,
        )  # fmt: skip
        return IngressResult(ack.bad_format(service, exc.case, exc.message))

    service = _service_for(path, parsed)
    merchant = await merchant_by_provider_id(parsed.merchant_id)
    if merchant is None or merchant["status"] == "INACTIVE":
        await store_rejected(
            protocol="SNAP", source_path=path, merchant_account_id=None,
            reason="unknown_merchant", payload=None,
            partner_reference=parsed.partner_reference,
        )  # fmt: skip
        return IngressResult(ack.not_found(service))

    try:
        event = verify_callback(
            parsed,
            headers=headers,
            raw_body=raw_body,
            path=path,
            query=query,
            url=url,
            merchant=merchant,
            source_path=path,
        )
    except SignatureError as exc:
        await store_rejected(
            protocol="SNAP", source_path=path, merchant_account_id=merchant["id"],
            reason=exc.reason, payload=None, provider_status=parsed.raw_status,
            partner_reference=parsed.partner_reference,
        )  # fmt: skip
        return IngressResult(ack.invalid_signature(service))

    final = await finalize_event(event)
    if final.try_later:
        await reject_unverifiable(event, final.try_later)
        return IngressResult(ack.gateway_error(service))

    result = final.process
    assert result is not None
    if result.payment:
        service = ack.METHOD_SERVICE.get(result.payment["payment_method"], service)
    nudge = result.job_enqueued

    outcome = result.outcome
    if (
        outcome == ProcessOutcome.DUPLICATE
        and settings.SNAP_DUPLICATE_ACK_MODE.upper() == "CONFLICT"
    ):
        return IngressResult(ack.duplicate(service))
    if outcome in (ProcessOutcome.UNMATCHED,):
        return IngressResult(ack.not_found(service))
    if outcome == ProcessOutcome.AMOUNT_MISMATCH:
        return IngressResult(ack.invalid_amount(service))
    if outcome == ProcessOutcome.CONTRADICTION and result.payment:
        if result.payment["status"] == "EXPIRED":
            return IngressResult(ack.expired(service))
    return IngressResult(ack.success(service, parsed.ack_echo), nudge=nudge)
