"""Redaction of personal data and secrets from stored provider payloads."""

from __future__ import annotations

from typing import Any

_PII_KEYS = {
    "virtualaccountname",
    "virtualaccountemail",
    "virtualaccountphone",
    "customername",
    "customeremail",
    "customerphone",
    "phoneno",
    "email",
    "msisdn",
    "cust_name",
    "cust_no",
    "customer_name",
    "name",
}
_SECRET_PARTS = ("signature", "password", "secret", "token", "authorization")


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _PII_KEYS:
                out[key] = "[REDACTED]"
            elif any(part in lowered for part in _SECRET_PARTS):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_payload(item)
        return out
    if isinstance(value, list):
        return [redact_payload(v) for v in value]
    return value
