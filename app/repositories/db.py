"""Thin helpers over AsyncConnection for raw SQL."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

Row = dict[str, Any]
_SQL_DIR = Path(__file__).parent / "sql"


def load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text()


def to_json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"), default=str)


async def fetch_one(
    conn: AsyncConnection, sql: str, params: Mapping[str, Any] | None = None
) -> Row | None:
    result = await conn.execute(text(sql), params or {})
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def fetch_all(
    conn: AsyncConnection, sql: str, params: Mapping[str, Any] | None = None
) -> list[Row]:
    result = await conn.execute(text(sql), params or {})
    return [dict(r) for r in result.mappings().all()]


async def execute(conn: AsyncConnection, sql: str, params: Mapping[str, Any] | None = None) -> int:
    result = await conn.execute(text(sql), params or {})
    return result.rowcount or 0
