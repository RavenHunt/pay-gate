"""Legacy notification parsing (TRD 9.3). Authenticity comes from the inquiry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.adapters.faspay.base import InquiryPolicy, VerifiedEvent
from app.adapters.faspay.credentials import legacy_credentials
from app.adapters.faspay.legacy.mappers import legacy_to_rupiah
from app.adapters.faspay.legacy.signing import legacy_notification_signature
from app.adapters.faspay.status_map import map_legacy_status
from app.config import ConfigError, get_settings
from app.utils.hashing import constant_time_equals, sha256_hex
from app.utils.redact import redact_payload
from app.utils.timeutil import JAKARTA

REQUIRED_FIELDS = ("request", "trx_id", "merchant_id", "bill_no")


class LegacyFormatError(Exception):
    pass


class LegacySignatureError(Exception):
    pass


@dataclass
class ParsedLegacy:
    merchant_id: str
    bill_no: str
    trx_id: str
    status_code: str
    amount: int | None
    paid_at: datetime | None
    channel_code: str | None
    payload: dict[str, Any]


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or isinstance(value, (dict, list, bool)) or not str(value).strip():
        raise LegacyFormatError(f"Missing or invalid field {key}")
    return str(value).strip()


def _paid_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JAKARTA)
    except ValueError:
        return None


def parse_notification(payload: Any) -> ParsedLegacy:
    if not isinstance(payload, dict):
        raise LegacyFormatError("Body must be a JSON object")
    values = {key: _text(payload, key) for key in REQUIRED_FIELDS}
    amount: int | None = None
    for key in ("payment_total", "bill_total"):
        if payload.get(key) not in (None, ""):
            try:
                amount = legacy_to_rupiah(payload[key])
            except ValueError as exc:
                raise LegacyFormatError(f"Invalid amount in {key}") from exc
            break
    status = payload.get("payment_status_code")
    channel = payload.get("payment_channel_uid") or payload.get("payment_channel")
    return ParsedLegacy(
        merchant_id=values["merchant_id"],
        bill_no=values["bill_no"],
        trx_id=values["trx_id"],
        status_code="" if status is None else str(status).strip(),
        amount=amount,
        paid_at=_paid_at(payload.get("payment_date")),
        channel_code=str(channel).strip() if channel else None,
        payload=payload,
    )


def verify_callback(
    parsed: ParsedLegacy, *, merchant: dict[str, Any], source_path: str
) -> VerifiedEvent:
    settings = get_settings()
    signature_valid = False
    if settings.LEGACY_VERIFY_NOTIFICATION_SIGNATURE:
        sent = str(parsed.payload.get("signature") or "")
        try:
            creds = legacy_credentials(merchant["credential_ref"])
        except ConfigError as exc:
            raise LegacySignatureError("credentials_unavailable") from exc
        expected = legacy_notification_signature(
            creds.user_id, creds.password, parsed.bill_no, parsed.status_code
        )
        if not sent or not constant_time_equals(sent.lower(), expected):
            raise LegacySignatureError("invalid_signature")
        signature_valid = True

    mapping = map_legacy_status(parsed.status_code)
    fingerprint = sha256_hex(
        "|".join(["LEGACY", str(merchant["id"]), parsed.bill_no, parsed.trx_id, parsed.status_code])
    )
    return VerifiedEvent(
        protocol="LEGACY",
        source_path=source_path,
        merchant=merchant,
        shape="LEGACY",
        partner_reference=parsed.bill_no,
        provider_reference=parsed.trx_id,
        amount=parsed.amount,
        currency="IDR" if parsed.amount is not None else None,
        provider_status=parsed.status_code,
        status=mapping.status,
        mapping=mapping,
        paid_at=parsed.paid_at,
        channel_code=parsed.channel_code,
        fingerprint=fingerprint,
        payload_redacted=redact_payload(parsed.payload),
        signature_valid=signature_valid,
        verification_method="LEGACY_INQUIRY",
        # Never mark a payment SUCCEEDED from the notification body alone.
        inquiry_policy=InquiryPolicy.ALWAYS,
    )
