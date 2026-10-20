"""Legacy driver: Post Data Transaction and Inquiry Status Payment."""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.faspay.base import (
    CancelResult,
    CreateContext,
    CreateOutcome,
    CreateResult,
    InquiryOutcome,
    InquiryResult,
    InquiryTarget,
)
from app.adapters.faspay.credentials import legacy_credentials
from app.adapters.faspay.http import classify_exception, get_http_client, safe_json
from app.adapters.faspay.legacy import mappers
from app.adapters.faspay.status_map import StatusMapping, map_legacy_status
from app.config import get_settings
from app.domain import Protocol
from app.utils.hashing import sha256_hex

LEGACY_SUCCESS = "00"


class LegacyDriver:
    protocol = Protocol.LEGACY

    def normalize_status(self, raw: object) -> StatusMapping:
        return map_legacy_status(raw)

    async def _post(self, path: str, body: dict[str, Any], timeout: float) -> httpx.Response:
        url = get_settings().faspay_base_url().rstrip("/") + path
        return await get_http_client().post(url, json=body, timeout=timeout)

    async def create_payment(self, ctx: CreateContext) -> CreateResult:
        settings = get_settings()
        creds = legacy_credentials(ctx.merchant["credential_ref"])
        body = mappers.build_create_body(ctx, creds)
        safe_body = {k: v for k, v in body.items() if k != "signature"}
        fingerprint = sha256_hex(repr(sorted(safe_body.items())))
        try:
            response = await self._post(
                mappers.CREATE_PATH, body, settings.FASPAY_CREATE_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001
            not_sent, category = classify_exception(exc)
            return CreateResult(
                CreateOutcome.FAILED if not_sent else CreateOutcome.UNKNOWN,
                error_category=category,
                request_fingerprint=fingerprint,
            )

        data = safe_json(response)
        code = str((data or {}).get("response_code") or "")
        desc = (data or {}).get("response_desc")

        def result(outcome: CreateOutcome, **extra: Any) -> CreateResult:
            return CreateResult(
                outcome,
                response_code=code or None,
                response_desc=str(desc) if desc else None,
                http_status=response.status_code,
                request_fingerprint=fingerprint,
                **extra,
            )

        if data is None or response.status_code >= 500 or response.status_code == 408:
            return result(CreateOutcome.UNKNOWN, error_category="provider_error")
        if code == LEGACY_SUCCESS:
            reference, instruction = mappers.parse_create_response(ctx, data)
            if not reference:
                return result(CreateOutcome.UNKNOWN, error_category="malformed_success")
            return result(
                CreateOutcome.SUCCEEDED, provider_reference=reference, instruction=instruction
            )
        if response.status_code in (401, 403):
            return result(CreateOutcome.FAILED, error_category="provider_auth")
        return result(CreateOutcome.FAILED, error_category="provider_rejected")

    async def inquire_payment(self, target: InquiryTarget) -> InquiryResult:
        settings = get_settings()
        if not target.provider_reference:
            return InquiryResult(InquiryOutcome.NOT_INQUIRABLE, error_category="no_provider_ref")
        creds = legacy_credentials(target.merchant["credential_ref"])
        body = mappers.build_inquiry_body(target, creds)
        try:
            response = await self._post(
                mappers.INQUIRY_PATH, body, settings.FASPAY_INQUIRY_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001
            _, category = classify_exception(exc)
            return InquiryResult(InquiryOutcome.ERROR, error_category=category)

        data = safe_json(response)
        if data is None or response.status_code >= 500:
            return InquiryResult(
                InquiryOutcome.ERROR,
                http_status=response.status_code,
                error_category="provider_error",
            )
        code = str(data.get("response_code") or "")
        raw_status = data.get("payment_status_code")
        if code != LEGACY_SUCCESS and raw_status in (None, ""):
            return InquiryResult(
                InquiryOutcome.ERROR,
                response_code=code or None,
                http_status=response.status_code,
                error_category="provider_rejected",
            )
        mapping = self.normalize_status(raw_status)
        return InquiryResult(
            InquiryOutcome.OK,
            status=mapping.status,
            provider_status=None if raw_status is None else str(raw_status),
            provider_reference=str(data.get("trx_id") or target.provider_reference),
            response_code=code or None,
            http_status=response.status_code,
            mapping=mapping,
        )

    async def cancel_payment(self, target: InquiryTarget) -> CancelResult:
        return CancelResult(supported=False)
