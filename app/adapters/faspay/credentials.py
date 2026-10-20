"""Credentials come from environment variables; the database stores only the prefix."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import ConfigError, get_settings, resolve_secret


def _clean(value: str) -> str:
    return value.strip().replace("\\n", "\n")


def _body(value: str) -> bytes:
    lines = [ln.strip() for ln in value.splitlines() if ln.strip() and "-----" not in ln]
    return base64.b64decode("".join(lines))


@lru_cache(maxsize=64)
def load_private_key(raw: str) -> rsa.RSAPrivateKey:
    """Accepts a full PEM or the bare base64 body used by the existing integration."""
    value = _clean(raw)
    try:
        if "-----BEGIN" in value:
            key = serialization.load_pem_private_key(value.encode(), password=None)
        else:
            key = serialization.load_der_private_key(_body(value), password=None)
    except (ValueError, TypeError) as exc:
        raise ConfigError("Invalid RSA private key material") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ConfigError("SNAP signing requires an RSA private key")
    return key


@lru_cache(maxsize=64)
def load_public_key(raw: str) -> rsa.RSAPublicKey:
    """Accepts a PEM public key, a PEM certificate, or a bare base64 body."""
    value = _clean(raw)
    try:
        if "-----BEGIN CERTIFICATE" in value:
            key: Any = x509.load_pem_x509_certificate(value.encode()).public_key()
        elif "-----BEGIN" in value:
            key = serialization.load_pem_public_key(value.encode())
        else:
            der = _body(value)
            try:
                key = serialization.load_der_public_key(der)
            except ValueError:
                key = x509.load_der_x509_certificate(der).public_key()
    except (ValueError, TypeError) as exc:
        raise ConfigError("Invalid Faspay public key material") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise ConfigError("Faspay public key must be RSA")
    return key


def snap_private_key(prefix: str) -> rsa.RSAPrivateKey:
    return load_private_key(resolve_secret(f"{prefix}_PRIVATE_KEY"))


def faspay_public_key(prefix: str) -> rsa.RSAPublicKey:
    raw = resolve_secret(f"{prefix}_FASPAY_PUBLIC_KEY", required=False)
    if not raw:
        raw = get_settings().SNAP_FASPAY_PUBLIC_KEY
    if not raw:
        raise ConfigError(f"Environment variable {prefix}_FASPAY_PUBLIC_KEY is not set")
    return load_public_key(raw)


@dataclass(frozen=True)
class LegacyCredentials:
    user_id: str
    password: str


def legacy_credentials(prefix: str) -> LegacyCredentials:
    return LegacyCredentials(
        user_id=resolve_secret(f"{prefix}_LEGACY_USER"),
        password=resolve_secret(f"{prefix}_LEGACY_PASSWORD"),
    )
