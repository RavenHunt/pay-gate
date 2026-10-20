from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories.db import Row, fetch_one

# route_code defaults to the client's default route for the method; a partial
# unique index guarantees at most one active match, so no ordering is needed.
_RESOLVE = """
SELECT r.id AS route_id, r.route_code, r.merchant_account_id, r.payment_channel_id,
       ch.payment_method, ch.channel_code, ch.protocol, ch.display_name,
       ch.provider_category, ch.status AS channel_status,
       m.merchant_code, m.provider, m.environment, m.credential_ref, m.partner_id,
       m.provider_merchant_id, m.merchant_name, m.status AS merchant_status
FROM client_merchant_routes r
JOIN payment_channels ch ON ch.id = r.payment_channel_id
JOIN merchant_accounts m ON m.id = r.merchant_account_id
WHERE r.client_id = :client_id
  AND r.payment_method = :method
  AND r.channel_code = :channel_code
  AND r.status = 'ACTIVE'
  AND r.route_code = COALESCE(
        CAST(:route_code AS VARCHAR),
        (SELECT d.route_code FROM client_merchant_routes d
          WHERE d.client_id = :client_id AND d.payment_method = :method
            AND d.is_default AND d.status = 'ACTIVE'))
"""


async def resolve_route(
    conn: AsyncConnection,
    client_id: uuid.UUID,
    method: str,
    channel_code: str,
    route_code: str | None,
) -> Row | None:
    return await fetch_one(
        conn,
        _RESOLVE,
        {
            "client_id": client_id,
            "method": method,
            "channel_code": channel_code,
            "route_code": route_code,
        },
    )
