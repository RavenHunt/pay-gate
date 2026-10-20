from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories.db import Row, fetch_one

_COLUMNS = (
    "id, merchant_code, provider, environment, credential_ref, partner_id, "
    "provider_merchant_id, merchant_name, status"
)


async def get_by_provider_merchant_id(
    conn: AsyncConnection, environment: str, provider_merchant_id: str
) -> Row | None:
    return await fetch_one(
        conn,
        f"SELECT {_COLUMNS} FROM merchant_accounts "
        "WHERE environment = :env AND provider_merchant_id = :mid",
        {"env": environment, "mid": provider_merchant_id},
    )


async def get_merchant(conn: AsyncConnection, merchant_id: uuid.UUID) -> Row | None:
    return await fetch_one(
        conn, f"SELECT {_COLUMNS} FROM merchant_accounts WHERE id = :id", {"id": merchant_id}
    )
