from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories.db import Row, execute, fetch_all, fetch_one, to_json

_PAYMENT_COLUMNS = """
    p.id, p.client_id, p.merchant_account_id, p.payment_channel_id, p.protocol, p.channel_code,
    p.merchant_reference, p.provider_partner_reference, p.provider_reference, p.amount,
    p.currency, p.payment_method, p.status, p.provider_status, p.instruction,
    p.metadata AS metadata, p.expires_at, p.paid_at, p.created_at, p.updated_at
"""


async def insert_payment(
    conn: AsyncConnection,
    *,
    payment_id: uuid.UUID,
    client_id: uuid.UUID,
    route: Row,
    merchant_reference: str,
    partner_reference: str,
    amount: int,
    metadata: dict[str, str] | None,
    expires_at: datetime,
) -> None:
    await execute(
        conn,
        """
        INSERT INTO payment_transactions
          (id, client_id, merchant_account_id, payment_channel_id, protocol, channel_code,
           merchant_reference, provider_partner_reference, amount, currency, payment_method,
           status, metadata, expires_at)
        VALUES
          (:id, :client_id, :merchant_account_id, :payment_channel_id, :protocol, :channel_code,
           :merchant_reference, :partner_reference, :amount, 'IDR', :payment_method,
           'CREATED', CAST(:metadata AS jsonb), :expires_at)
        """,
        {
            "id": payment_id,
            "client_id": client_id,
            "merchant_account_id": route["merchant_account_id"],
            "payment_channel_id": route["payment_channel_id"],
            "protocol": route["protocol"],
            "channel_code": route["channel_code"],
            "merchant_reference": merchant_reference,
            "partner_reference": partner_reference,
            "amount": amount,
            "payment_method": route["payment_method"],
            "metadata": to_json(metadata or {}),
            "expires_at": expires_at,
        },
    )


async def get_for_client(
    conn: AsyncConnection, client_id: uuid.UUID, payment_id: uuid.UUID
) -> Row | None:
    return await fetch_one(
        conn,
        f"SELECT {_PAYMENT_COLUMNS} FROM payment_transactions p "
        "WHERE p.client_id = :client_id AND p.id = :id",
        {"client_id": client_id, "id": payment_id},
    )


async def get_latest_by_reference(
    conn: AsyncConnection, client_id: uuid.UUID, merchant_reference: str
) -> Row | None:
    return await fetch_one(
        conn,
        f"SELECT {_PAYMENT_COLUMNS} FROM payment_transactions p "
        "WHERE p.client_id = :client_id AND p.merchant_reference = :ref "
        "ORDER BY p.created_at DESC LIMIT 1",
        {"client_id": client_id, "ref": merchant_reference},
    )


async def get_by_id(
    conn: AsyncConnection, payment_id: uuid.UUID, *, for_update: bool = False
) -> Row | None:
    lock = " FOR UPDATE OF p" if for_update else ""
    return await fetch_one(
        conn,
        f"SELECT {_PAYMENT_COLUMNS} FROM payment_transactions p WHERE p.id = :id{lock}",
        {"id": payment_id},
    )


async def find_by_partner_reference(
    conn: AsyncConnection,
    merchant_account_id: uuid.UUID,
    partner_reference: str,
    *,
    for_update: bool = False,
) -> Row | None:
    lock = " FOR UPDATE OF p" if for_update else ""
    return await fetch_one(
        conn,
        f"SELECT {_PAYMENT_COLUMNS} FROM payment_transactions p "
        "WHERE p.merchant_account_id = :m AND p.provider_partner_reference = :ref" + lock,
        {"m": merchant_account_id, "ref": partner_reference},
    )


async def find_by_provider_reference(
    conn: AsyncConnection,
    merchant_account_id: uuid.UUID,
    protocol: str,
    provider_reference: str,
    *,
    for_update: bool = False,
) -> Row | None:
    lock = " FOR UPDATE OF p" if for_update else ""
    return await fetch_one(
        conn,
        f"SELECT {_PAYMENT_COLUMNS} FROM payment_transactions p "
        "WHERE p.merchant_account_id = :m AND p.protocol = :protocol "
        "AND p.provider_reference = :ref" + lock,
        {"m": merchant_account_id, "protocol": protocol, "ref": provider_reference},
    )


async def update_after_create(
    conn: AsyncConnection,
    payment_id: uuid.UUID,
    *,
    provider_reference: str | None,
    instruction: dict[str, Any] | None,
    provider_status: str | None,
) -> None:
    await execute(
        conn,
        """
        UPDATE payment_transactions
        SET provider_reference = COALESCE(:provider_reference, provider_reference),
            instruction = COALESCE(CAST(:instruction AS jsonb), instruction),
            provider_status = COALESCE(:provider_status, provider_status),
            updated_at = now()
        WHERE id = :id
        """,
        {
            "id": payment_id,
            "provider_reference": provider_reference,
            "instruction": to_json(instruction),
            "provider_status": provider_status,
        },
    )


async def guarded_status_update(
    conn: AsyncConnection,
    payment_id: uuid.UUID,
    *,
    from_status: str,
    to_status: str,
    provider_status: str | None,
    paid_at: datetime | None,
) -> bool:
    """Conditional update: only applies if the status is still `from_status`."""
    changed = await execute(
        conn,
        """
        UPDATE payment_transactions
        SET status = :to_status,
            provider_status = COALESCE(:provider_status, provider_status),
            paid_at = COALESCE(:paid_at, paid_at),
            updated_at = now()
        WHERE id = :id AND status = :from_status
        """,
        {
            "id": payment_id,
            "from_status": from_status,
            "to_status": to_status,
            "provider_status": provider_status,
            "paid_at": paid_at,
        },
    )
    return changed == 1


async def set_provider_fields(
    conn: AsyncConnection,
    payment_id: uuid.UUID,
    *,
    provider_status: str | None,
    provider_reference: str | None,
) -> None:
    await execute(
        conn,
        """
        UPDATE payment_transactions
        SET provider_status = COALESCE(:provider_status, provider_status),
            provider_reference = COALESCE(provider_reference, :provider_reference),
            updated_at = now()
        WHERE id = :id
        """,
        {
            "id": payment_id,
            "provider_status": provider_status,
            "provider_reference": provider_reference,
        },
    )


async def list_recent_events(
    conn: AsyncConnection, payment_id: uuid.UUID, limit: int = 20
) -> list[Row]:
    return await fetch_all(
        conn,
        "SELECT id, event_type, status_from, status_to, source, occurred_at "
        "FROM payment_events WHERE payment_id = :id ORDER BY occurred_at DESC LIMIT :limit",
        {"id": payment_id, "limit": limit},
    )
