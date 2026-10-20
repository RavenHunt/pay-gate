from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.observability import correlation_id_var
from app.repositories.db import execute, to_json
from app.utils.ids import uuid7
from app.utils.redact import redact_payload


async def write_audit(
    conn: AsyncConnection,
    *,
    entity_type: str,
    entity_id: str,
    actor_type: str,
    actor_id: str | None,
    action: str,
    payment_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await execute(
        conn,
        """
        INSERT INTO audit_logs
          (id, payment_id, entity_type, entity_id, actor_type, actor_id, action,
           correlation_id, metadata_redacted)
        VALUES (:id, :payment_id, :entity_type, :entity_id, :actor_type, :actor_id, :action,
                :correlation_id, CAST(:metadata AS jsonb))
        """,
        {
            "id": uuid7(),
            "payment_id": payment_id,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "correlation_id": correlation_id_var.get() or None,
            "metadata": to_json(redact_payload(metadata) if metadata else None),
        },
    )
