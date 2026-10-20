"""Route resolution (TRD 8): one active route, one protocol, never client chosen."""

from __future__ import annotations

import uuid
from typing import Any

from app.config import get_settings
from app.database import transaction
from app.exceptions import GatewayError, forbidden
from app.repositories import routes as routes_repo
from app.utils.ttlcache import TTLCache

_route_cache: TTLCache[tuple[Any, ...], dict[str, Any]] | None = None


def _cache() -> TTLCache[tuple[Any, ...], dict[str, Any]]:
    global _route_cache
    if _route_cache is None:
        _route_cache = TTLCache(get_settings().STATUS_CACHE_TTL_SECONDS)
    return _route_cache


def reset_route_cache() -> None:
    global _route_cache
    _route_cache = None


async def resolve_route(
    client_id: uuid.UUID, method: str, channel_code: str, route_code: str | None
) -> dict[str, Any]:
    key = (client_id, method, channel_code, route_code)
    route = _cache().get(key)
    if route is None:
        async with transaction() as conn:
            route = await routes_repo.resolve_route(
                conn, client_id, method, channel_code, route_code
            )
        if route is None:
            raise forbidden(
                "ROUTE_NOT_AUTHORIZED",
                "No active route is available for this payment method and channel.",
            )
        _cache().set(key, route)

    settings = get_settings()
    if route["channel_status"] != "ACTIVE":
        raise forbidden("ROUTE_NOT_AUTHORIZED", "The channel is not active.")
    if route["provider"] != "FASPAY" or route["environment"] != settings.environment:
        raise forbidden("ROUTE_NOT_AUTHORIZED", "The route does not belong to this environment.")
    if route["merchant_status"] == "MAINTENANCE":
        raise GatewayError(503, "MERCHANT_MAINTENANCE", "The merchant account is in maintenance.")
    if route["merchant_status"] != "ACTIVE":
        raise forbidden("ROUTE_NOT_AUTHORIZED", "The merchant account is not active.")
    return route
