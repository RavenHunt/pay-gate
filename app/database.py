"""Single async engine for Neon PostgreSQL (pooled endpoint).

Under PgBouncer transaction pooling the asyncpg statement cache must be off
and a NullPool is used, exactly as in ki-cf-v5. Runtime code works with
short-lived `AsyncConnection` transactions and raw SQL.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import ConfigError, get_settings

_engine: AsyncEngine | None = None


def build_engine_args(database_url: str) -> tuple[URL, dict[str, Any]]:
    """Translate a libpq style URL (Neon) into an asyncpg SQLAlchemy URL."""
    url = make_url(database_url)
    if url.drivername in ("postgres", "postgresql", "postgresql+psycopg2"):
        url = url.set(drivername="postgresql+asyncpg")
    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    url = url.set(query=query)
    connect_args: dict[str, Any] = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4().hex}__",
    }
    if sslmode in ("require", "verify-ca", "verify-full"):
        connect_args["ssl"] = True
    return url, connect_args


def configure_engine(database_url: str | None = None) -> AsyncEngine:
    global _engine
    settings = get_settings()
    dsn = database_url or settings.DATABASE_URL
    if not dsn:
        raise ConfigError("DATABASE_URL is not set")
    url, connect_args = build_engine_args(dsn)
    _engine = create_async_engine(
        url, poolclass=NullPool, echo=settings.DB_ECHO, connect_args=connect_args
    )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return configure_engine()
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncConnection]:
    """One connection, one short transaction (commit on exit, rollback on error)."""
    async with get_engine().begin() as conn:
        yield conn


async def ping() -> bool:
    from sqlalchemy import text

    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
