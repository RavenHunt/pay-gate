"""Consumer webhook delivery: signing, SSRF guard, and the DELIVER_WEBHOOK handler."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import ConfigError, get_settings, resolve_secret
from app.database import transaction
from app.observability import alert, emit_metric, get_logger
from app.repositories import deliveries as deliveries_repo
from app.schemas.payments import event_payload
from app.services.job_errors import DeadJob, RetryJob
from app.services.retry import compute_backoff
from app.utils.hashing import hmac_sha256_hex, minify_json
from app.utils.ids import event_public_id

log = get_logger("delivery")

_client: httpx.AsyncClient | None = None


def get_delivery_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(follow_redirects=False)
    return _client


def set_delivery_client(client: httpx.AsyncClient | None) -> None:
    """Test hook."""
    global _client
    _client = client


class DestinationError(Exception):
    pass


@dataclass
class ResolvedDestination:
    url: str  # URL to request (host replaced by the validated IP when pinned)
    host_header: str
    sni_hostname: str | None


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # includes the 169.254.169.254 metadata address
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


async def resolve_destination(url: str) -> ResolvedDestination:
    """HTTPS only; every resolved address must be public; the request is pinned to it."""
    settings = get_settings()
    parts = urlsplit(url)
    if settings.ALLOW_INSECURE_WEBHOOK_URLS:
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise DestinationError("invalid_destination")
        return ResolvedDestination(url, parts.netloc, None)
    if parts.scheme != "https" or not parts.hostname:
        raise DestinationError("destination_not_https")
    if parts.username or parts.password:
        raise DestinationError("destination_has_credentials")

    host = parts.hostname
    port = parts.port or 443
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise DestinationError("destination_unresolvable") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or any(_ip_blocked(ip) for ip in addresses):
        raise DestinationError("destination_blocked")

    ip = addresses[0]
    ip_text = f"[{ip}]" if ip.version == 6 else str(ip)
    path = parts.path or "/"
    pinned = f"https://{ip_text}:{port}{path}" + (f"?{parts.query}" if parts.query else "")
    host_header = host if port == 443 else f"{host}:{port}"
    return ResolvedDestination(pinned, host_header, host)


def current_secrets(secret_ref: str) -> list[str]:
    """Current secret first; `<name>_PREV` is signed with too while a rotation is open."""
    secrets = [resolve_secret(secret_ref)]
    previous = resolve_secret(f"{secret_ref}_PREV", required=False)
    if previous:
        secrets.append(previous)
    return secrets


def signature_header(secrets: list[str], timestamp: str, body: bytes) -> str:
    signed = timestamp.encode() + b"." + body
    return ",".join(f"v1={hmac_sha256_hex(secret, signed)}" for secret in secrets)


@dataclass
class HttpResult:
    status: int | None
    error_category: str | None
    retry_after: float | None = None


async def send(
    destination: ResolvedDestination, body: bytes, headers: dict[str, str]
) -> HttpResult:
    settings = get_settings()
    request_headers = {"Content-Type": "application/json", "Host": destination.host_header}
    request_headers.update(headers)
    extensions = {"sni_hostname": destination.sni_hostname} if destination.sni_hostname else {}
    try:
        async with get_delivery_client().stream(
            "POST",
            destination.url,
            content=body,
            headers=request_headers,
            timeout=settings.DELIVERY_TIMEOUT_SECONDS,
            extensions=extensions,
        ) as response:
            read = 0
            async for chunk in response.aiter_bytes():
                read += len(chunk)
                if read >= settings.DELIVERY_MAX_RESPONSE_BYTES:
                    break  # response size cap
            retry_after = None
            raw = response.headers.get("retry-after")
            if raw and raw.isdigit():
                retry_after = float(raw)
            return HttpResult(response.status_code, None, retry_after)
    except httpx.TimeoutException:
        return HttpResult(None, "timeout")
    except httpx.TransportError:
        return HttpResult(None, "network_error")


async def handle_deliver_webhook(job: dict[str, Any]) -> None:
    settings = get_settings()
    delivery_id = job["ref_id"]
    async with transaction() as conn:
        ctx = await deliveries_repo.get_delivery_context(conn, delivery_id)
    if ctx is None or ctx["delivery_status"] == "DELIVERED":
        return  # idempotent: already delivered or purged
    if ctx["client_status"] != "ACTIVE":
        raise DeadJob("client_inactive")

    try:
        secrets = current_secrets(ctx["webhook_secret_ref"] or "")
    except ConfigError as exc:
        alert("webhook_secret_unavailable", client_id=str(ctx["client_id"]))
        await _record(delivery_id, "RETRY_SCHEDULED", None, "secret_missing", job)
        raise RetryJob("secret_missing") from exc

    try:
        destination = await resolve_destination(ctx["destination_url"])
    except DestinationError as exc:
        alert("webhook_destination_rejected", client_id=str(ctx["client_id"]), reason=str(exc))
        raise DeadJob(str(exc)) from exc

    payload = event_payload(
        event_id=event_public_id(ctx["payment_event_id"]),
        event_type=ctx["event_type"],
        ctx=ctx,
        occurred_at=ctx["occurred_at"],
    )
    body = minify_json(payload)
    timestamp = str(int(time.time()))
    headers = {
        "X-Gateway-Event-Id": payload["event_id"],  # stable across retries
        "X-Gateway-Timestamp": timestamp,
        "X-Gateway-Signature": signature_header(secrets, timestamp, body),
    }
    result = await send(destination, body, headers)
    emit_metric(
        "delivery_attempt",
        protocol=ctx["protocol"],
        status=result.status,
        error_category=result.error_category,
    )

    if result.status is not None and 200 <= result.status < 300:
        await _record(delivery_id, "DELIVERED", result.status, None, job)
        return

    retryable = result.status is None or result.status in (408, 429) or result.status >= 500
    category = result.error_category or f"http_{result.status}"
    if not retryable:
        raise DeadJob(category)

    delay = compute_backoff(
        job["attempts"],
        base=settings.RETRY_BASE_SECONDS,
        cap=settings.RETRY_CAP_SECONDS,
        jitter_fraction=settings.RETRY_JITTER_FRACTION,
    )
    if result.retry_after:
        delay = max(delay, min(result.retry_after, settings.RETRY_CAP_SECONDS))
    await _record(delivery_id, "RETRY_SCHEDULED", result.status, category, job, delay)
    raise RetryJob(category, delay)


async def _record(
    delivery_id: Any,
    status: str,
    http_status: int | None,
    category: str | None,
    job: dict[str, Any],
    delay: float | None = None,
) -> None:
    if status == "RETRY_SCHEDULED" and job["attempts"] >= job["max_attempts"]:
        status, delay = "DEAD_LETTER", None  # the runner will mark the job DEAD
    async with transaction() as conn:
        await deliveries_repo.record_attempt(
            conn,
            delivery_id,
            delivery_status=status,
            http_status=http_status,
            error_category=category,
            next_retry_delay_seconds=delay,
        )


async def on_delivery_dead(job: dict[str, Any], category: str) -> None:
    async with transaction() as conn:
        await deliveries_repo.set_status(conn, job["ref_id"], "DEAD_LETTER")
    alert("delivery_dead_letter", delivery_id=str(job["ref_id"]), category=category)
