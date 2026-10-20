"""POST /v1/oauth/token: OAuth 2.0 client credentials (TRD 5.1)."""

from __future__ import annotations

import base64
import binascii
import time
from collections import defaultdict, deque
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import transaction
from app.exceptions import OAuthError
from app.repositories import clients as clients_repo
from app.schemas.oauth import TokenResponse
from app.services.security import verify_client_secret
from app.services.tokens import issue_access_token

router = APIRouter(tags=["oauth"])

_failures: dict[str, deque[float]] = defaultdict(deque)
_BASIC_CHALLENGE = {"WWW-Authenticate": 'Basic realm="gateway"'}


def _throttled(client_id: str) -> bool:
    """Best-effort per-instance limiter; Vercel Firewall rate limits are the real control."""
    window = _failures[client_id]
    now = time.monotonic()
    while window and now - window[0] > 60:
        window.popleft()
    return len(window) >= get_settings().OAUTH_FAILED_ATTEMPTS_PER_MINUTE


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None
    client_id, sep, secret = decoded.partition(":")
    return (client_id, secret) if sep else None


@router.post("/v1/oauth/token", response_model=TokenResponse)
async def issue_token(request: Request) -> JSONResponse:
    form = await request.form()
    if form.get("grant_type") is None:
        raise OAuthError("invalid_request", "grant_type is required.")
    if form.get("grant_type") != "client_credentials":
        raise OAuthError("unsupported_grant_type", "Only client_credentials is supported.")

    credentials = _basic_credentials(request)
    if credentials is None:
        client_id, secret = str(form.get("client_id") or ""), str(form.get("client_secret") or "")
    else:
        client_id, secret = credentials
    if not client_id or not secret:
        raise OAuthError(
            "invalid_client", "Client authentication failed.", 401, headers=_BASIC_CHALLENGE
        )
    if _throttled(client_id):
        raise OAuthError("invalid_request", "Too many failed attempts.", 429)

    async with transaction() as conn:
        row = await clients_repo.get_credential_by_oauth_id(conn, client_id)
    # Always verify (against a dummy hash when unknown) to keep timing uniform.
    secret_ok = verify_client_secret(secret, row["secret_hash"] if row else None)
    usable = (
        row is not None
        and secret_ok
        and row["credential_status"] == "ACTIVE"
        and row["client_status"] == "ACTIVE"
        and (row["expires_at"] is None or row["expires_at"] > datetime.now(UTC))
    )
    if not usable or row is None:
        _failures[client_id].append(time.monotonic())
        raise OAuthError(
            "invalid_client", "Client authentication failed.", 401, headers=_BASIC_CHALLENGE
        )

    allowed = list(row["scopes"] or [])
    requested = str(form.get("scope") or "").split()
    if requested:
        if not set(requested) <= set(allowed):
            raise OAuthError("invalid_scope", "A requested scope is not allowed.")
        scopes = [s for s in allowed if s in requested]
    else:
        scopes = allowed

    token, expires_in = issue_access_token(
        client_id=row["client_id"], credential_id=row["credential_id"], scopes=scopes
    )
    async with transaction() as conn:
        await clients_repo.touch_credential(conn, row["credential_id"])
    body = TokenResponse(
        access_token=token, expires_in=expires_in, scope=" ".join(scopes)
    ).model_dump()
    return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
