"""Identifier helpers: UUIDv7 and the compact provider partner reference."""

from __future__ import annotations

import os
import secrets
import time
import uuid

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
PARTNER_REFERENCE_LENGTH = 16


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 version 7)."""
    millis = int(time.time() * 1000)
    raw = bytearray(millis.to_bytes(6, "big") + os.urandom(10))
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(raw))


def new_partner_reference() -> str:
    """16 random characters, the strictest documented provider length."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(PARTNER_REFERENCE_LENGTH))


def event_public_id(payment_event_id: uuid.UUID | str) -> str:
    return f"evt_{payment_event_id}"
