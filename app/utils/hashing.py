"""Hash, HMAC and constant-time helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def b64url_sha256(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def hmac_sha256_hex(key: str | bytes, message: str | bytes) -> str:
    if isinstance(key, str):
        key = key.encode()
    if isinstance(message, str):
        message = message.encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for fingerprints."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def minify_json(value: Any) -> bytes:
    """Compact JSON bytes; serialize once and reuse for both hash and body."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
