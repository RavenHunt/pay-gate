"""webhook_events (inbox) and payment_events (outbound business events)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories.db import Row, execute, fetch_all, fetch_one, to_json
from app.utils.ids import uuid7


async def insert_inbox_event(
    conn: AsyncConnection,
    *,
    merchant_account_id: uuid.UUID | None,
    protocol: str,
    source_path: str,
    provider_event_key: str | None,
    fingerprint: str,
    event_type: str,
    provider_status_code: str | None,
    processing_status: str,
    signature_valid: bool,
    verification_method: str,
    payload: dict[str, Any] | None,
) -> uuid.UUID | None:
    """Returns the new id, or None when the fingerprint already exists (duplicate)."""
    row = await fetch_one(
        conn,
        """
        INSERT INTO webhook_events
          (id, merchant_account_id, protocol, source_path, provider_event_key, event_fingerprint,
           event_type, provider_status_code, processing_status, signature_valid,
           verification_method, payload_redacted, received_at)
        VALUES
          (:id, :merchant_account_id, :protocol, :source_path, :provider_event_key, :fingerprint,
           :event_type, :provider_status_code, :processing_status, :signature_valid,
           :verification_method, CAST(:payload AS jsonb), now())
        ON CONFLICT (event_fingerprint) DO NOTHING
        RETURNING id
        """,
        {
            "id": uuid7(),
            "merchant_account_id": merchant_account_id,
            "protocol": protocol,
            "source_path": source_path[:128],
            "provider_event_key": provider_event_key,
            "fingerprint": fingerprint,
            "event_type": event_type,
            "provider_status_code": provider_status_code,
            "processing_status": processing_status,
            "signature_valid": signature_valid,
            "verification_method": verification_method,
            "payload": to_json(payload),
        },
    )
    return row["id"] if row else None


async def get_by_fingerprint(conn: AsyncConnection, fingerprint: str) -> Row | None:
    return await fetch_one(
        conn,
        """
        SELECT id, processing_status, payment_id, merchant_account_id, protocol, source_path
        FROM webhook_events WHERE event_fingerprint = :fp
        """,
        {"fp": fingerprint},
    )


async def get_event(conn: AsyncConnection, event_id: uuid.UUID) -> Row | None:
    return await fetch_one(
        conn,
        """
        SELECT id, merchant_account_id, payment_id, protocol, source_path, provider_event_key,
               event_fingerprint, event_type, provider_status_code, processing_status,
               signature_valid, verification_method, payload_redacted, received_at, processed_at
        FROM webhook_events WHERE id = :id
        """,
        {"id": event_id},
    )


async def set_event_result(
    conn: AsyncConnection,
    event_id: uuid.UUID,
    *,
    processing_status: str,
    payment_id: uuid.UUID | None = None,
    signature_valid: bool | None = None,
    verification_method: str | None = None,
    payload: dict[str, Any] | None = None,
    merchant_account_id: uuid.UUID | None = None,
) -> None:
    await execute(
        conn,
        """
        UPDATE webhook_events
        SET processing_status = CAST(:status AS VARCHAR),
            payment_id = COALESCE(:payment_id, payment_id),
            merchant_account_id = COALESCE(:merchant_account_id, merchant_account_id),
            signature_valid = COALESCE(:signature_valid, signature_valid),
            verification_method = COALESCE(:verification_method, verification_method),
            payload_redacted = COALESCE(CAST(:payload AS jsonb), payload_redacted),
            processed_at = CASE WHEN CAST(:status AS VARCHAR) = 'PROCESSED'
                THEN now() ELSE processed_at END
        WHERE id = :id
        """,
        {
            "id": event_id,
            "status": processing_status,
            "payment_id": payment_id,
            "merchant_account_id": merchant_account_id,
            "signature_valid": signature_valid,
            "verification_method": verification_method,
            "payload": to_json(payload),
        },
    )


async def unmatched_for_partner_reference(
    conn: AsyncConnection, merchant_account_id: uuid.UUID, partner_reference: str
) -> list[Row]:
    return await fetch_all(
        conn,
        """
        SELECT id FROM webhook_events
        WHERE processing_status = 'UNMATCHED' AND payment_id IS NULL
          AND merchant_account_id = :m AND provider_event_key = :ref
        """,
        {"m": merchant_account_id, "ref": partner_reference},
    )


async def insert_payment_event(
    conn: AsyncConnection,
    *,
    payment_id: uuid.UUID,
    webhook_event_id: uuid.UUID | None,
    event_type: str,
    status_from: str | None,
    status_to: str,
    source: str,
    occurred_at: datetime | None = None,
) -> uuid.UUID:
    event_id = uuid7()
    await execute(
        conn,
        """
        INSERT INTO payment_events
          (id, payment_id, webhook_event_id, event_type, status_from, status_to, source,
           occurred_at, created_at)
        VALUES (:id, :payment_id, :webhook_event_id, :event_type, :status_from, :status_to,
                :source, COALESCE(:occurred_at, now()), now())
        """,
        {
            "id": event_id,
            "payment_id": payment_id,
            "webhook_event_id": webhook_event_id,
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "source": source,
            "occurred_at": occurred_at,
        },
    )
    return event_id
