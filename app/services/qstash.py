"""QStash: verify inbound job-run requests, and publish best-effort nudges."""

from __future__ import annotations

import time

import jwt
from starlette.requests import Request

from app.adapters.faspay.http import get_http_client
from app.config import get_settings
from app.observability import get_logger
from app.utils.hashing import b64url_sha256

log = get_logger("qstash")
_last_nudge = 0.0


def expected_url(request: Request) -> str:
    """The URL QStash signed. PUBLIC_BASE_URL wins over what the proxy reports."""
    base = get_settings().PUBLIC_BASE_URL.rstrip("/")
    if base:
        return base + request.url.path
    return str(request.url.replace(query=""))


def verify_signature(signature: str | None, body: bytes, url: str) -> bool:
    if not signature:
        return False
    s = get_settings()
    keys = [k for k in (s.QSTASH_CURRENT_SIGNING_KEY, s.QSTASH_NEXT_SIGNING_KEY) if k]
    for key in keys:
        try:
            claims = jwt.decode(
                signature,
                key,
                algorithms=["HS256"],
                issuer="Upstash",
                options={"require": ["exp", "nbf", "sub", "iss"]},
                leeway=5,
            )
        except jwt.PyJWTError:
            continue
        if claims.get("sub") != url:
            continue
        claimed_body = str(claims.get("body", "")).rstrip("=")
        if claimed_body != b64url_sha256(body).rstrip("="):
            continue
        return True
    return False


async def nudge(max_jobs: int = 10) -> bool:
    """Best-effort one-off publish to /internal/jobs/run. The schedule is the safety net."""
    global _last_nudge
    s = get_settings()
    if not (s.QSTASH_NUDGE_ENABLED and s.QSTASH_TOKEN and s.PUBLIC_BASE_URL):
        return False
    now = time.monotonic()
    if now - _last_nudge < s.QSTASH_NUDGE_MIN_INTERVAL_SECONDS:
        return False  # coalesce bursts to protect the QStash message quota
    _last_nudge = now
    target = s.PUBLIC_BASE_URL.rstrip("/") + "/internal/jobs/run"
    try:
        response = await get_http_client().post(
            f"{s.QSTASH_URL.rstrip('/')}/v2/publish/{target}",
            headers={
                "Authorization": f"Bearer {s.QSTASH_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"max_jobs": max_jobs},
            timeout=s.QSTASH_NUDGE_TIMEOUT_SECONDS,
        )
        return response.is_success
    except Exception as exc:  # noqa: BLE001 - never let a nudge failure surface
        log.warning("nudge_failed", error_type=type(exc).__name__)
        return False
