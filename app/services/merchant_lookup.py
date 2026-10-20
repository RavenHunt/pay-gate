"""Merchant resolution for inbound callbacks, with a short in-memory TTL cache."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.database import transaction
from app.repositories import merchants as merchants_repo
from app.utils.ttlcache import TTLCache

_cache: TTLCache[str, dict[str, Any]] | None = None


def reset_merchant_cache() -> None:
    global _cache
    _cache = None


async def merchant_by_provider_id(provider_merchant_id: str) -> dict[str, Any] | None:
    global _cache
    settings = get_settings()
    if _cache is None:
        _cache = TTLCache(settings.STATUS_CACHE_TTL_SECONDS)
    key = f"{settings.environment}:{provider_merchant_id}"
    merchant = _cache.get(key)
    if merchant is not None:
        return merchant
    async with transaction() as conn:
        merchant = await merchants_repo.get_by_provider_merchant_id(
            conn, settings.environment, provider_merchant_id
        )
    if merchant is not None:
        _cache.set(key, merchant)
    return merchant
