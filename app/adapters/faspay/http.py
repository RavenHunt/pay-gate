"""One module-level httpx client per instance, plus outcome classification."""

from __future__ import annotations

from typing import Any

import httpx

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(follow_redirects=False)
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Test hook: inject a client backed by httpx.MockTransport."""
    global _client
    _client = client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def classify_exception(exc: Exception) -> tuple[bool, str]:
    """Returns (definitely_not_sent, error_category).

    A connection that was never established proves the provider saw nothing, so
    the create is a confirmed failure. Anything after the request may have been
    written is ambiguous.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True, "connect_error"
    if isinstance(exc, httpx.PoolTimeout):
        return True, "pool_timeout"
    if isinstance(exc, httpx.TimeoutException):
        return False, "timeout"
    if isinstance(exc, httpx.TransportError):
        return False, "network_error"
    return False, "unexpected_error"


def safe_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
