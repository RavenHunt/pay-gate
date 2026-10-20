"""SNAP notification parsing and verification (TRD 9.2)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.adapters.faspay.base import InquiryPolicy, VerifiedEvent
from app.adapters.faspay.credentials import faspay_public_key
from app.adapters.faspay.snap import signing
from app.adapters.faspay.snap.mappers import snap_to_rupiah
from app.adapters.faspay.status_map import map_snap_status
from app.config import ConfigError, get_settings
from app.utils.hashing import sha256_hex
from app.utils.redact import redact_payload
from app.utils.timeutil import JAKARTA, parse_iso, utcnow

SHAPE_VA = "VA"
SHAPE_REFERENCE = "REFERENCE"


class NotificationFormatError(Exception):
    """`case` is the SNAP case code: 01 invalid field format, 02 missing mandatory field."""

    def __init__(self, case: str, message: str) -> None:
        super().__init__(message)
        self.case = case
        self.message = message


class SignatureError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SignatureOutcome(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    MISSING = "MISSING"  # no header, or no key to verify with


@dataclass
class ParsedNotification:
    shape: str
    merchant_id: str
    partner_reference: str
    provider_reference: str | None
    amount: int | None
    currency: str | None
    raw_status: str
    paid_at: datetime | None
    channel_code: str | None
    extras: dict[str, Any]
    fingerprint_parts: list[str]
    payload: dict[str, Any]
    ack_echo: dict[str, Any] = field(default_factory=dict)


def _get(data: Any, *path: str) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _required(data: dict[str, Any], *paths: tuple[str, ...]) -> list[Any]:
    values = []
    for path in paths:
        value = _get(data, *path)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise NotificationFormatError("02", f"Missing mandatory field {'.'.join(path)}")
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise NotificationFormatError("01", f"Invalid field format {'.'.join(path)}")
        values.append(str(value).strip())
    return values


def _amount(data: dict[str, Any], *path: str) -> tuple[int | None, str | None]:
    node = _get(data, *path)
    if node is None:
        return None, None
    if not isinstance(node, dict) or "value" not in node:
        raise NotificationFormatError("01", f"Invalid field format {'.'.join(path)}")
    try:
        value = snap_to_rupiah(node["value"])
    except ValueError as exc:
        raise NotificationFormatError("01", f"Invalid amount {'.'.join(path)}") from exc
    currency = node.get("currency")
    return value, (str(currency).strip().upper() if currency else None)


def _paid_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T", 1) if " " in value.strip() else value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JAKARTA)


def parse_notification(payload: Any) -> ParsedNotification:
    """Chooses the parser by payload shape, never by route or channelCode."""
    if not isinstance(payload, dict):
        raise NotificationFormatError("01", "Body must be a JSON object")

    if "virtualAccountNo" in payload:
        _required(
            payload,
            ("partnerServiceId",),
            ("customerNo",),
            ("virtualAccountNo",),
            ("paymentRequestId",),
            ("trxId",),
            ("additionalInfo", "paymentDate"),
            ("additionalInfo", "channelCode"),
            ("additionalInfo", "merchantId"),
            ("additionalInfo", "latestTransactionStatus"),
        )
        amount, currency = _amount(payload, "paidAmount")
        va_no = str(payload["virtualAccountNo"]).strip()
        trx_id = str(payload["trxId"]).strip()
        status = str(_get(payload, "additionalInfo", "latestTransactionStatus")).strip()
        return ParsedNotification(
            shape=SHAPE_VA,
            merchant_id=str(_get(payload, "additionalInfo", "merchantId")).strip(),
            partner_reference=trx_id,
            provider_reference=va_no,
            amount=amount,
            currency=currency,
            raw_status=status,
            paid_at=_paid_at(_get(payload, "additionalInfo", "paymentDate")),
            channel_code=str(_get(payload, "additionalInfo", "channelCode")).strip(),
            extras={
                "partnerServiceId": payload["partnerServiceId"],
                "customerNo": payload["customerNo"],
                "virtualAccountNo": va_no,
                "paymentRequestId": str(payload["paymentRequestId"]).strip(),
            },
            fingerprint_parts=[
                va_no,
                str(payload["paymentRequestId"]).strip() or trx_id,
                status,
                "" if amount is None else str(amount),
            ],
            payload=payload,
            ack_echo={
                "partnerServiceId": payload["partnerServiceId"],
                "customerNo": payload["customerNo"],
                "virtualAccountNo": payload["virtualAccountNo"],
                "paymentRequestId": payload["paymentRequestId"],
                "paidAmount": payload.get("paidAmount"),
            },
        )

    if "originalPartnerReferenceNo" in payload:
        _required(
            payload,
            ("originalPartnerReferenceNo",),
            ("originalReferenceNo",),
            ("latestTransactionStatus",),
            ("additionalInfo", "paymentDate"),
            ("additionalInfo", "channelCode"),
        )
        merchant_id = _get(payload, "additionalInfo", "merchantId") or payload.get("merchantId")
        if merchant_id is None or not str(merchant_id).strip():
            raise NotificationFormatError("02", "Missing mandatory field merchantId")
        amount, currency = _amount(payload, "amount")
        partner = str(payload["originalPartnerReferenceNo"]).strip()
        provider = str(payload["originalReferenceNo"]).strip()
        status = str(payload["latestTransactionStatus"]).strip()
        return ParsedNotification(
            shape=SHAPE_REFERENCE,
            merchant_id=str(merchant_id).strip(),
            partner_reference=partner,
            provider_reference=provider,
            amount=amount,
            currency=currency,
            raw_status=status,
            paid_at=_paid_at(_get(payload, "additionalInfo", "paymentDate")),
            channel_code=str(_get(payload, "additionalInfo", "channelCode")).strip(),
            extras={},
            fingerprint_parts=[
                provider,
                partner,
                status,
                "" if amount is None else str(amount),
            ],
            payload=payload,
        )

    raise NotificationFormatError("02", "Unrecognized notification payload")


def make_fingerprint(merchant_account_id: str, parsed: ParsedNotification) -> str:
    return sha256_hex(
        "|".join(["SNAP", merchant_account_id, parsed.shape, *parsed.fingerprint_parts])
    )


# ── Signature verification ──────────────────────────────────────


def _signature_header(headers: Mapping[str, str]) -> str | None:
    for name in get_settings().SNAP_CALLBACK_SIGNATURE_HEADERS.split(","):
        value = headers.get(name.strip())
        if value:
            return value
    return None


def _body_variants(raw: bytes) -> list[bytes]:
    variants = [raw]
    try:
        parsed = json.loads(raw)
    except ValueError:
        return variants
    for ensure_ascii in (False, True):
        candidate = json.dumps(parsed, separators=(",", ":"), ensure_ascii=ensure_ascii).encode()
        if candidate not in variants:
            variants.append(candidate)
    return variants


def _path_candidates(path: str, query: str, url: str) -> list[str]:
    out: list[str] = []
    for mode in get_settings().SNAP_CALLBACK_PATH_MODES.split(","):
        mode = mode.strip()
        if mode == "path":
            out.append(path)
        elif mode == "path_query":
            out.append(f"{path}?{query}" if query else path)
        elif mode == "full_url":
            out.append(url)
    return list(dict.fromkeys(out)) or [path]


def check_signature(
    headers: Mapping[str, str],
    raw_body: bytes,
    path: str,
    query: str,
    url: str,
    merchant: dict[str, Any],
) -> SignatureOutcome:
    signature = _signature_header(headers)
    if not signature:
        return SignatureOutcome.MISSING
    try:
        public_key = faspay_public_key(merchant["credential_ref"])
    except ConfigError:
        return SignatureOutcome.MISSING
    timestamp = headers.get("X-TIMESTAMP")
    if not timestamp:
        return SignatureOutcome.INVALID
    try:
        sent_at = parse_iso(timestamp)
    except ValueError:
        return SignatureOutcome.INVALID
    tolerance = get_settings().SNAP_CALLBACK_TIMESTAMP_TOLERANCE_SECONDS
    if abs((utcnow() - sent_at).total_seconds()) > tolerance:
        return SignatureOutcome.INVALID
    for candidate_path in _path_candidates(path, query, url):
        for body in _body_variants(raw_body):
            message = signing.string_to_sign("POST", candidate_path, body, timestamp)
            if signing.verify(public_key, signature, message):
                return SignatureOutcome.VALID
    return SignatureOutcome.INVALID


def verify_callback(
    parsed: ParsedNotification,
    *,
    headers: Mapping[str, str],
    raw_body: bytes,
    path: str,
    query: str,
    url: str,
    merchant: dict[str, Any],
    source_path: str,
) -> VerifiedEvent:
    """Raises SignatureError when the notification may not be trusted at all."""
    settings = get_settings()
    mode = settings.CALLBACK_VERIFY_MODE.upper()
    outcome = check_signature(headers, raw_body, path, query, url, merchant)
    if outcome is SignatureOutcome.INVALID:
        raise SignatureError("invalid_signature")
    if outcome is SignatureOutcome.MISSING:
        if mode == "SIGNATURE_AND_INQUIRY" and settings.SNAP_ALLOW_UNSIGNED_WITH_INQUIRY:
            method, valid, policy = "SNAP_INQUIRY_ONLY", False, InquiryPolicy.ALWAYS
        else:
            raise SignatureError("signature_missing")
    elif mode == "SIGNATURE_AND_INQUIRY":
        method, valid, policy = "SNAP_SIGNATURE", True, InquiryPolicy.ON_SUCCESS
    else:
        method, valid, policy = "SNAP_SIGNATURE", True, InquiryPolicy.NONE

    mapping = map_snap_status(parsed.raw_status)
    fingerprint = make_fingerprint(str(merchant["id"]), parsed)
    return VerifiedEvent(
        protocol="SNAP",
        source_path=source_path,
        merchant=merchant,
        shape=parsed.shape,
        partner_reference=parsed.partner_reference,
        provider_reference=parsed.provider_reference,
        amount=parsed.amount,
        currency=parsed.currency,
        provider_status=parsed.raw_status,
        status=mapping.status,
        mapping=mapping,
        paid_at=parsed.paid_at,
        channel_code=parsed.channel_code,
        fingerprint=fingerprint,
        payload_redacted=redact_payload(parsed.payload),
        signature_valid=valid,
        verification_method=method,
        inquiry_policy=policy,
        extras=parsed.extras,
    )
