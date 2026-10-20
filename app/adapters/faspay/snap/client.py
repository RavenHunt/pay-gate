"""SNAP driver: signed requests, response classification, inquiry."""

from __future__ import annotations

import uuid
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
from app.adapters.faspay.credentials import snap_private_key
from app.adapters.faspay.http import classify_exception, get_http_client, safe_json
from app.adapters.faspay.snap import mappers, signing
from app.adapters.faspay.status_map import StatusMapping, map_snap_status
from app.config import ConfigError, get_settings
from app.domain import Protocol
from app.observability import get_logger
from app.utils.hashing import minify_json, sha256_hex
from app.utils.timeutil import snap_timestamp

log = get_logger("faspay.snap")


class SnapDriver:
    protocol = Protocol.SNAP

    def normalize_status(self, raw: object) -> StatusMapping:
        return map_snap_status(raw)

    async def _post(
        self,
        merchant: dict[str, Any],
        path: str,
        body: dict[str, Any],
        timeout: float,
        external_id: str,
    ) -> httpx.Response:
        settings = get_settings()
        payload = minify_json(body)  # serialize once: the hash and the body are identical
        timestamp = snap_timestamp()
        key = snap_private_key(merchant["credential_ref"])
        signature = signing.sign(key, signing.string_to_sign("POST", path, payload, timestamp))
        headers = {
            "Content-Type": "application/json",
            "X-TIMESTAMP": timestamp,
            "X-SIGNATURE": signature,
            "X-PARTNER-ID": merchant.get("partner_id") or "",
            "X-EXTERNAL-ID": external_id,
            "CHANNEL-ID": settings.SNAP_CHANNEL_ID,
        }
        url = settings.faspay_base_url().rstrip("/") + path
        return await get_http_client().post(url, content=payload, headers=headers, timeout=timeout)

    # ── Create ────────────────────────────────────────────────────
    async def create_payment(self, ctx: CreateContext) -> CreateResult:
        settings = get_settings()
        path = mappers.CREATE_PATHS[ctx.payment_method]
        body = mappers.build_create_body(ctx)
        fingerprint = sha256_hex(minify_json(body))
        try:
            response = await self._post(
                ctx.merchant, path, body, settings.FASPAY_CREATE_TIMEOUT_SECONDS, ctx.external_id
            )
        except ConfigError:
            raise  # missing credentials: nothing was sent (handled by the caller)
        except Exception as exc:  # noqa: BLE001 - classified below
            not_sent, category = classify_exception(exc)
            return CreateResult(
                CreateOutcome.FAILED if not_sent else CreateOutcome.UNKNOWN,
                error_category=category,
                request_fingerprint=fingerprint,
            )
        return self._classify_create(ctx, response, fingerprint)

    def _classify_create(
        self, ctx: CreateContext, response: httpx.Response, fingerprint: str
    ) -> CreateResult:
        data = safe_json(response)
        code = str((data or {}).get("responseCode") or "")
        desc = (data or {}).get("responseMessage")
        http = response.status_code

        def result(outcome: CreateOutcome, **extra: Any) -> CreateResult:
            return CreateResult(
                outcome,
                response_code=code or None,
                response_desc=str(desc) if desc else None,
                http_status=http,
                request_fingerprint=fingerprint,
                **extra,
            )

        if data is not None and code.startswith("2"):
            reference, instruction = mappers.parse_create_response(
                ctx.payment_method, data, ctx.expires_at
            )
            if not reference:
                # The provider reported success but returned no identifier: it exists.
                return result(CreateOutcome.UNKNOWN, error_category="malformed_success")
            return result(
                CreateOutcome.SUCCEEDED, provider_reference=reference, instruction=instruction
            )
        if http == 409 or code.startswith("409"):
            return result(CreateOutcome.UNKNOWN, error_category="duplicate_reference")
        if http == 408 or http >= 500 or code.startswith("5") or data is None:
            return result(CreateOutcome.UNKNOWN, error_category="provider_error")
        if http in (401, 403):
            return result(CreateOutcome.FAILED, error_category="provider_auth")
        if http == 429:
            return result(CreateOutcome.FAILED, error_category="rate_limited")
        return result(CreateOutcome.FAILED, error_category="provider_rejected")

    # ── Inquiry ───────────────────────────────────────────────────
    async def inquire_payment(self, target: InquiryTarget) -> InquiryResult:
        settings = get_settings()
        body = mappers.build_inquiry_body(target)
        if body is None:
            return InquiryResult(InquiryOutcome.NOT_INQUIRABLE, error_category="no_provider_ref")
        path = mappers.INQUIRY_PATHS[target.payment_method]
        try:
            response = await self._post(
                target.merchant,
                path,
                body,
                settings.FASPAY_INQUIRY_TIMEOUT_SECONDS,
                uuid.uuid4().hex,
            )
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            _, category = classify_exception(exc)
            return InquiryResult(InquiryOutcome.ERROR, error_category=category)

        data = safe_json(response)
        code = str((data or {}).get("responseCode") or "")
        if data is None or response.status_code >= 500:
            return InquiryResult(
                InquiryOutcome.ERROR,
                http_status=response.status_code,
                error_category="provider_error",
            )
        if code.startswith("404") or response.status_code == 404:
            return InquiryResult(
                InquiryOutcome.NOT_FOUND,
                response_code=code or None,
                http_status=response.status_code,
            )
        if not code.startswith("2"):
            return InquiryResult(
                InquiryOutcome.ERROR,
                response_code=code or None,
                http_status=response.status_code,
                error_category="provider_rejected",
            )
        raw_status = mappers.extract_status(target.payment_method, data)
        mapping = self.normalize_status(raw_status)
        reference = None
        if target.payment_method != "VA":
            reference = data.get("referenceNo") or data.get("originalReferenceNo")
        return InquiryResult(
            InquiryOutcome.OK,
            status=mapping.status,
            provider_status=None if raw_status is None else str(raw_status),
            provider_reference=str(reference) if reference else None,
            amount=mappers.extract_amount(data),
            paid_at=None,
            response_code=code,
            http_status=response.status_code,
            mapping=mapping,
        )

    async def cancel_payment(self, target: InquiryTarget) -> CancelResult:
        return CancelResult(supported=False)
