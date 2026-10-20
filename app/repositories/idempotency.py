from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories.db import Row, execute, fetch_one, to_json
from app.utils.ids import uuid7


async def get(conn: AsyncConnection, client_id: uuid.UUID, key: str) -> Row | None:
    return await fetch_one(
        conn,
        """
        SELECT id, request_fingerprint, operation_status, payment_id, response_snapshot
        FROM api_idempotency_keys WHERE client_id = :client_id AND idempotency_key = :key
        """,
        {"client_id": client_id, "key": key},
    )


async def claim(conn: AsyncConnection, *, client_id: uuid.UUID, key: str, fingerprint: str) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING: the unique constraint serializes concurrent callers.

    A concurrent loser waits for the winner's transaction, then sees no row returned.
    """
    row = await fetch_one(
        conn,
        """
        INSERT INTO api_idempotency_keys
          (id, client_id, idempotency_key, request_fingerprint, operation_status)
        VALUES (:id, :client_id, :key, :fingerprint, 'IN_PROGRESS')
        ON CONFLICT (client_id, idempotency_key) DO NOTHING
        RETURNING id
        """,
        {"id": uuid7(), "client_id": client_id, "key": key, "fingerprint": fingerprint},
    )
    return row is not None


async def attach_payment(
    conn: AsyncConnection, client_id: uuid.UUID, key: str, payment_id: uuid.UUID
) -> None:
    await execute(
        conn,
        "UPDATE api_idempotency_keys SET payment_id = :payment_id "
        "WHERE client_id = :client_id AND idempotency_key = :key",
        {"payment_id": payment_id, "client_id": client_id, "key": key},
    )


async def resolve(
    conn: AsyncConnection,
    client_id: uuid.UUID,
    key: str,
    *,
    operation_status: str,
    snapshot: dict[str, Any] | None,
) -> None:
    # COMPLETED and FAILED are resolved (retention clock starts); UNKNOWN is never expired.
    await execute(
        conn,
        """
        UPDATE api_idempotency_keys
        SET operation_status = CAST(:status AS VARCHAR),
            response_snapshot = CAST(:snapshot AS jsonb),
            resolved_at = CASE WHEN CAST(:status AS VARCHAR) IN ('COMPLETED', 'FAILED')
                THEN now() ELSE NULL END
        WHERE client_id = :client_id AND idempotency_key = :key
        """,
        {
            "client_id": client_id,
            "key": key,
            "status": operation_status,
            "snapshot": to_json(snapshot),
        },
    )


async def resolve_for_payment(
    conn: AsyncConnection, payment_id: uuid.UUID, *, operation_status: str
) -> None:
    """Called when an UNKNOWN payment is later resolved so retention can purge the key."""
    await execute(
        conn,
        """
        UPDATE api_idempotency_keys
        SET operation_status = CAST(:status AS VARCHAR), resolved_at = now()
        WHERE payment_id = :payment_id AND operation_status IN ('UNKNOWN', 'IN_PROGRESS')
        """,
        {"payment_id": payment_id, "status": operation_status},
    )


async def mark_unknown_for_payment(conn: AsyncConnection, payment_id: uuid.UUID) -> None:
    """A create whose outcome was never recorded is unresolved: keep it, never expire it."""
    await execute(
        conn,
        "UPDATE api_idempotency_keys SET operation_status = 'UNKNOWN' "
        "WHERE payment_id = :payment_id AND operation_status = 'IN_PROGRESS'",
        {"payment_id": payment_id},
    )
