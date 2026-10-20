from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories.db import Row, execute, fetch_one


async def get_credential_by_oauth_id(conn: AsyncConnection, oauth_client_id: str) -> Row | None:
    return await fetch_one(
        conn,
        """
        SELECT cc.id AS credential_id, cc.client_id, cc.secret_hash, cc.scopes,
               cc.status AS credential_status, cc.expires_at,
               c.status AS client_status, c.app_code
        FROM client_credentials cc
        JOIN payment_clients c ON c.id = cc.client_id
        WHERE cc.oauth_client_id = :oauth_client_id
        """,
        {"oauth_client_id": oauth_client_id},
    )


async def get_auth_status(
    conn: AsyncConnection, client_id: uuid.UUID, credential_id: uuid.UUID
) -> Row | None:
    return await fetch_one(
        conn,
        """
        SELECT c.id AS client_id, c.app_code, c.status AS client_status,
               cc.status AS credential_status, cc.expires_at, cc.revoked_at, cc.scopes
        FROM client_credentials cc
        JOIN payment_clients c ON c.id = cc.client_id
        WHERE cc.id = :credential_id AND cc.client_id = :client_id
        """,
        {"credential_id": credential_id, "client_id": client_id},
    )


async def touch_credential(conn: AsyncConnection, credential_id: uuid.UUID) -> None:
    # Throttled so token issuance at high rates does not turn into a write storm.
    await execute(
        conn,
        """
        UPDATE client_credentials SET last_used_at = now()
        WHERE id = :id AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')
        """,
        {"id": credential_id},
    )


async def get_client(conn: AsyncConnection, client_id: uuid.UUID) -> Row | None:
    return await fetch_one(
        conn,
        "SELECT id, app_code, name, status, webhook_url, webhook_secret_ref "
        "FROM payment_clients WHERE id = :id",
        {"id": client_id},
    )
