"""Access tokens: short-lived JWTs signed by the gateway (TRD 5.1).

RS256/ES256 with `kid` rotation in every deployed environment. HS256 with
`OAUTH_JWT_SECRET` is accepted for local development and tests only.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.config import ConfigError, get_settings


@dataclass(frozen=True)
class _KeySet:
    alg: str
    sign_key: Any
    sign_kid: str
    verify_keys: dict[str, tuple[str, Any]]  # kid -> (alg, key)


_keyset: _KeySet | None = None


def reset_token_keys() -> None:
    global _keyset
    _keyset = None


def _pem(value: str) -> bytes:
    return value.strip().replace("\\n", "\n").encode()


def _alg_for(key: Any) -> str:
    if isinstance(key, rsa.RSAPrivateKey | rsa.RSAPublicKey):
        return "RS256"
    if isinstance(key, ec.EllipticCurvePrivateKey | ec.EllipticCurvePublicKey):
        return "ES256"
    raise ConfigError("OAuth signing key must be RSA or P-256 EC")


def _build_keyset() -> _KeySet:
    s = get_settings()
    if s.OAUTH_JWT_PRIVATE_KEY:
        private = serialization.load_pem_private_key(_pem(s.OAUTH_JWT_PRIVATE_KEY), password=None)
        alg = _alg_for(private)
        public = (
            serialization.load_pem_public_key(_pem(s.OAUTH_JWT_PUBLIC_KEY))
            if s.OAUTH_JWT_PUBLIC_KEY
            else private.public_key()
        )
        verify: dict[str, tuple[str, Any]] = {s.OAUTH_JWT_KID: (alg, public)}
        if s.OAUTH_JWT_PUBLIC_KEY_PREV and s.OAUTH_JWT_KID_PREV:
            prev = serialization.load_pem_public_key(_pem(s.OAUTH_JWT_PUBLIC_KEY_PREV))
            verify[s.OAUTH_JWT_KID_PREV] = (_alg_for(prev), prev)
        return _KeySet(alg, private, s.OAUTH_JWT_KID, verify)
    if s.OAUTH_JWT_SECRET:
        if s.is_production:
            raise ConfigError("HS256 OAuth secrets are not allowed in production")
        return _KeySet(
            "HS256",
            s.OAUTH_JWT_SECRET,
            s.OAUTH_JWT_KID,
            {s.OAUTH_JWT_KID: ("HS256", s.OAUTH_JWT_SECRET)},
        )
    raise ConfigError("Set OAUTH_JWT_PRIVATE_KEY (RS256/ES256), or OAUTH_JWT_SECRET for local dev")


def _keys() -> _KeySet:
    global _keyset
    if _keyset is None:
        _keyset = _build_keyset()
    return _keyset


def issue_access_token(
    *, client_id: uuid.UUID, credential_id: uuid.UUID, scopes: list[str]
) -> tuple[str, int]:
    s = get_settings()
    keys = _keys()
    now = int(time.time())
    claims = {
        "iss": s.OAUTH_ISSUER,
        "aud": s.OAUTH_AUDIENCE,
        "sub": str(client_id),
        "cred": str(credential_id),
        "iat": now,
        "exp": now + s.OAUTH_TOKEN_TTL_SECONDS,
        "jti": uuid.uuid4().hex,
        "scope": " ".join(scopes),
        "kid": keys.sign_kid,
    }
    token = jwt.encode(claims, keys.sign_key, algorithm=keys.alg, headers={"kid": keys.sign_kid})
    return token, s.OAUTH_TOKEN_TTL_SECONDS


def decode_access_token(token: str) -> dict[str, Any]:
    """Stateless validation; raises jwt.PyJWTError for any problem."""
    s = get_settings()
    keys = _keys()
    header = jwt.get_unverified_header(token)
    entry = keys.verify_keys.get(header.get("kid", ""))
    if entry is None:
        raise jwt.InvalidTokenError("unknown kid")
    alg, key = entry  # the algorithm comes from our key, never from the token header
    return jwt.decode(
        token,
        key,
        algorithms=[alg],
        audience=s.OAUTH_AUDIENCE,
        issuer=s.OAUTH_ISSUER,
        options={"require": ["exp", "iat", "sub", "cred", "scope", "iss", "aud"]},
    )
