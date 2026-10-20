"""FastAPI dependencies: client authentication (JWT), scopes, admin token."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
import structlog
from fastapi import Depends, Request

from app.config import get_settings
from app.database import transaction
from app.exceptions import GatewayError, forbidden, unauthorized
from app.repositories import clients as clients_repo
from app.services.security import verify_admin_token
from app.services.tokens import decode_access_token
from app.utils.ttlcache import TTLCache


@dataclass(frozen=True)
class AuthContext:
    client_id: uuid.UUID
    credential_id: uuid.UUID
    app_code: str
    scopes: frozenset[str]


_status_cache: TTLCache[tuple[uuid.UUID, uuid.UUID], dict[str, Any]] | None = None


def _cache() -> TTLCache[tuple[uuid.UUID, uuid.UUID], dict[str, Any]]:
    global _status_cache
    if _status_cache is None:
        _status_cache = TTLCache(get_settings().STATUS_CACHE_TTL_SECONDS)
    return _status_cache


def reset_auth_cache() -> None:
    global _status_cache
    _status_cache = None


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise unauthorized()
    return value.strip()


async def authenticate(request: Request) -> AuthContext:
    token = _bearer(request)
    try:
        claims = decode_access_token(token)
        client_id = uuid.UUID(claims["sub"])
        credential_id = uuid.UUID(claims["cred"])
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise unauthorized() from exc

    cache_key = (client_id, credential_id)
    row = _cache().get(cache_key)
    if row is None:
        async with transaction() as conn:
            row = await clients_repo.get_auth_status(conn, client_id, credential_id)
        if row is None:
            raise unauthorized()
        _cache().set(cache_key, row)

    # Revocation is enforced here (within the cache TTL), not only at token expiry.
    if row["credential_status"] != "ACTIVE" or row["revoked_at"] is not None:
        raise unauthorized()
    if row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC):
        raise unauthorized()
    if row["client_status"] != "ACTIVE":
        raise forbidden("CLIENT_INACTIVE", "The client is not active.")

    # Effective scopes: what the token carries, still allowed on the live credential.
    token_scopes = set(str(claims.get("scope", "")).split())
    scopes = frozenset(token_scopes & set(row["scopes"] or []))
    structlog.contextvars.bind_contextvars(client_id=str(client_id))
    return AuthContext(client_id, credential_id, row["app_code"], scopes)


def require_scope(scope: str) -> Callable[..., Awaitable[AuthContext]]:
    async def dependency(auth: AuthContext = Depends(authenticate)) -> AuthContext:
        if scope not in auth.scopes:
            raise forbidden("INSUFFICIENT_SCOPE", f"The token lacks the {scope} scope.")
        return auth

    return dependency


@dataclass(frozen=True)
class AdminContext:
    operator: str


async def require_admin(request: Request) -> AdminContext:
    token = _bearer(request)
    if not verify_admin_token(token):
        raise GatewayError(
            401,
            "UNAUTHORIZED",
            "Missing or invalid admin token.",
            headers={"WWW-Authenticate": 'Bearer realm="admin"'},
        )
    operator = request.headers.get("x-operator", "admin")[:64]
    return AdminContext(operator=operator)
