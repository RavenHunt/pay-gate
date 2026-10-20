from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories.db import Row, execute, fetch_one
from app.utils.ids import uuid7


async def insert_delivery(
    conn: AsyncConnection,
    *,
    payment_event_id: uuid.UUID,
    client_id: uuid.UUID,
    destination_url: str,
) -> uuid.UUID | None:
    row = await fetch_one(
        conn,
        """
        INSERT INTO webhook_deliveries
          (id, payment_event_id, client_id, destination_url, delivery_status, attempt_count)
        VALUES (:id, :event_id, :client_id, :url, 'PENDING', 0)
        ON CONFLICT (payment_event_id, client_id) DO NOTHING
        RETURNING id
        """,
        {
            "id": uuid7(),
            "event_id": payment_event_id,
            "client_id": client_id,
            "url": destination_url,
        },
    )
    return row["id"] if row else None


async def get_delivery_context(conn: AsyncConnection, delivery_id: uuid.UUID) -> Row | None:
    return await fetch_one(
        conn,
        """
        SELECT d.id AS delivery_id, d.delivery_status, d.destination_url, d.attempt_count,
               d.client_id, c.status AS client_status, c.webhook_secret_ref,
               e.id AS payment_event_id, e.event_type, e.status_to, e.occurred_at,
               p.id AS payment_id, p.merchant_reference, p.provider_reference, p.amount,
               p.currency, p.payment_method, p.channel_code, p.paid_at, p.metadata AS metadata,
               p.protocol
        FROM webhook_deliveries d
        JOIN payment_events e ON e.id = d.payment_event_id
        JOIN payment_transactions p ON p.id = e.payment_id
        JOIN payment_clients c ON c.id = d.client_id
        WHERE d.id = :id
        """,
        {"id": delivery_id},
    )


async def record_attempt(
    conn: AsyncConnection,
    delivery_id: uuid.UUID,
    *,
    delivery_status: str,
    http_status: int | None,
    error_category: str | None,
    next_retry_delay_seconds: float | None = None,
) -> None:
    await execute(
        conn,
        """
        UPDATE webhook_deliveries
        SET delivery_status = CAST(:status AS VARCHAR),
            attempt_count = attempt_count + 1,
            last_http_status = :http_status,
            last_error_category = :category,
            next_retry_at = CASE WHEN CAST(:delay AS double precision) IS NULL THEN NULL
                ELSE now() + make_interval(secs => CAST(:delay AS double precision)) END,
            delivered_at = CASE WHEN CAST(:status AS VARCHAR) = 'DELIVERED'
                THEN now() ELSE delivered_at END
        WHERE id = :id
        """,
        {
            "id": delivery_id,
            "status": delivery_status,
            "http_status": http_status,
            "category": error_category,
            "delay": next_retry_delay_seconds,
        },
    )


async def set_status(conn: AsyncConnection, delivery_id: uuid.UUID, status: str) -> None:
    await execute(
        conn,
        "UPDATE webhook_deliveries SET delivery_status = :status WHERE id = :id",
        {"id": delivery_id, "status": status},
    )


async def get_basic(conn: AsyncConnection, delivery_id: uuid.UUID) -> Row | None:
    return await fetch_one(
        conn,
        "SELECT id, payment_event_id, client_id, delivery_status FROM webhook_deliveries "
        "WHERE id = :id",
        {"id": delivery_id},
    )
