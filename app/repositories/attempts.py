from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories.db import execute, fetch_one
from app.utils.ids import uuid7


async def start_attempt(
    conn: AsyncConnection,
    *,
    payment_id: uuid.UUID,
    operation: str,
    protocol: str,
    external_id: str | None,
) -> uuid.UUID:
    row = await fetch_one(
        conn,
        """
        SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no
        FROM payment_attempts WHERE payment_id = :payment_id AND operation = :operation
        """,
        {"payment_id": payment_id, "operation": operation},
    )
    attempt_id = uuid7()
    await execute(
        conn,
        """
        INSERT INTO payment_attempts
          (id, payment_id, attempt_no, operation, protocol, external_id, outcome, started_at)
        VALUES (:id, :payment_id, :attempt_no, :operation, :protocol, :external_id,
                'PENDING', now())
        """,
        {
            "id": attempt_id,
            "payment_id": payment_id,
            "attempt_no": row["next_no"] if row else 1,
            "operation": operation,
            "protocol": protocol,
            "external_id": external_id,
        },
    )
    return attempt_id


async def finish_attempt(
    conn: AsyncConnection,
    attempt_id: uuid.UUID,
    *,
    outcome: str,
    http_status: int | None = None,
    provider_response_code: str | None = None,
    provider_reference: str | None = None,
    request_fingerprint: str | None = None,
    error_category: str | None = None,
) -> None:
    await execute(
        conn,
        """
        UPDATE payment_attempts
        SET outcome = :outcome, http_status = :http_status,
            provider_response_code = :code, provider_reference = :reference,
            request_fingerprint = :fingerprint, error_category = :category, finished_at = now()
        WHERE id = :id
        """,
        {
            "id": attempt_id,
            "outcome": outcome,
            "http_status": http_status,
            "code": provider_response_code,
            "reference": provider_reference,
            "fingerprint": request_fingerprint,
            "category": error_category,
        },
    )
