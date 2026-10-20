"""Client secret hashing and admin token verification."""

from __future__ import annotations

import secrets

from app.config import ConfigError, get_settings
from app.utils.hashing import constant_time_equals, hmac_sha256_hex, sha256_hex

_PREFIX = "hmac-sha256$"
# Compared against when the client id is unknown, so timing does not reveal existence.
_DUMMY_HASH = _PREFIX + "0" * 64


def hash_client_secret(secret: str) -> str:
    """Keyed HMAC-SHA-256 with a server pepper, for high-entropy generated secrets."""
    pepper = get_settings().CLIENT_SECRET_PEPPER
    if not pepper:
        raise ConfigError("CLIENT_SECRET_PEPPER is not set")
    return _PREFIX + hmac_sha256_hex(pepper, secret)


def verify_client_secret(secret: str, stored_hash: str | None) -> bool:
    candidate = hash_client_secret(secret)
    return constant_time_equals(candidate, stored_hash or _DUMMY_HASH)


def generate_client_secret() -> str:
    return secrets.token_urlsafe(32)


def verify_admin_token(token: str) -> bool:
    expected = get_settings().ADMIN_API_TOKEN_HASH.strip().lower()
    if not expected:
        return False
    return constant_time_equals(sha256_hex(token), expected)
