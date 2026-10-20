"""Audited administration CLI (no admin UI in the MVP).

    python -m scripts.admin --operator alice create-client \\
        --app-code FLUENCY_HUB --name "Fluency Hub"

Every change writes an audit_logs row (actor_type ADMIN). Route changes wait for
the 30 s route-cache TTL before reporting completion (use --no-wait to skip).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from app.config import get_settings
from app.database import dispose_engine, transaction
from app.domain import ALL_SCOPES
from app.repositories.audit import write_audit
from app.repositories.db import execute, fetch_all, fetch_one
from app.services.security import generate_client_secret, hash_client_secret
from app.utils.ids import uuid7

SEED_CHANNELS: list[tuple[str, str, str, str, str | None]] = [
    ("QRIS", "715", "SNAP", "QRIS 715", "qris"),
    ("QRIS", "711", "SNAP", "QRIS 711", "qris"),
    ("QRIS", "842", "SNAP", "QRIS 842", "qris"),
    ("QRIS", "836", "SNAP", "QRIS 836", "qris"),
    ("EWALLET", "722", "SNAP", "DANA (722)", "e-wallet"),
    ("EWALLET", "819", "SNAP", "DANA (819)", "e-wallet"),
    ("EWALLET", "716", "SNAP", "LinkAja (716)", "e-wallet"),
    ("EWALLET", "302", "SNAP", "LinkAja (302)", "e-wallet"),
    ("RETAIL", "707", "LEGACY", "Alfagroup", "retail"),
    ("RETAIL", "706", "LEGACY", "Indomaret", "retail"),
]

Command = Callable[[argparse.Namespace], Awaitable[None]]


def _validate_webhook_url(url: str, allow_insecure: bool) -> None:
    parts = urlsplit(url)
    if not parts.hostname or parts.username or parts.password:
        raise SystemExit("webhook url must have a host and no credentials")
    if parts.scheme != "https" and not allow_insecure:
        raise SystemExit("webhook url must be https")


async def _audit(
    conn: Any, args: argparse.Namespace, entity: str, entity_id: Any, action: str, meta: Any = None
) -> None:
    await write_audit(
        conn,
        entity_type=entity,
        entity_id=str(entity_id),
        actor_type="ADMIN",
        actor_id=args.operator,
        action=action,
        metadata=meta,
    )


async def _client_id(conn: Any, app_code: str) -> Any:
    row = await fetch_one(
        conn, "SELECT id FROM payment_clients WHERE app_code = :c", {"c": app_code}
    )
    if row is None:
        raise SystemExit(f"unknown client {app_code}")
    return row["id"]


async def _wait_for_cache(args: argparse.Namespace) -> None:
    ttl = get_settings().STATUS_CACHE_TTL_SECONDS
    if args.no_wait:
        print(f"note: route/status caches take up to {ttl}s to reflect this change")
        return
    print(f"waiting {ttl}s for caches to expire ...")
    await asyncio.sleep(ttl + 1)


async def seed_channels(args: argparse.Namespace) -> None:
    async with transaction() as conn:
        added = 0
        for method, code, protocol, name, category in SEED_CHANNELS:
            added += await execute(
                conn,
                """
                INSERT INTO payment_channels
                  (id, payment_method, channel_code, protocol, display_name, provider_category)
                VALUES (:id, :m, :c, :p, :n, :cat)
                ON CONFLICT (payment_method, channel_code, protocol) DO NOTHING
                """,
                {"id": uuid7(), "m": method, "c": code, "p": protocol, "n": name, "cat": category},
            )
        await _audit(conn, args, "CHANNEL", "seed", "seed_channels", {"added": added})
    print(f"seeded {added} channel rows (VA bank codes are per merchant: use add-channel)")


async def add_channel(args: argparse.Namespace) -> None:
    async with transaction() as conn:
        channel_id = uuid7()
        await execute(
            conn,
            """
            INSERT INTO payment_channels
              (id, payment_method, channel_code, protocol, display_name, provider_category)
            VALUES (:id, :m, :c, :p, :n, :cat)
            """,
            {
                "id": channel_id,
                "m": args.method,
                "c": args.channel_code,
                "p": args.protocol,
                "n": args.display_name,
                "cat": args.category,
            },
        )
        await _audit(conn, args, "CHANNEL", channel_id, "add_channel", vars(args) | {"cmd": None})
    print(f"channel {args.method}/{args.channel_code}/{args.protocol} added")


async def set_channel_status(args: argparse.Namespace) -> None:
    async with transaction() as conn:
        changed = await execute(
            conn,
            "UPDATE payment_channels SET status = :s, updated_at = now() "
            "WHERE payment_method = :m AND channel_code = :c AND protocol = :p",
            {"s": args.status, "m": args.method, "c": args.channel_code, "p": args.protocol},
        )
        await _audit(conn, args, "CHANNEL", f"{args.method}:{args.channel_code}", "set_status")
    print(f"{changed} channel row(s) updated")
    await _wait_for_cache(args)


async def create_client(args: argparse.Namespace) -> None:
    if args.webhook_url:
        _validate_webhook_url(args.webhook_url, args.allow_insecure)
    async with transaction() as conn:
        client_id = uuid7()
        await execute(
            conn,
            """
            INSERT INTO payment_clients (id, app_code, name, webhook_url, webhook_secret_ref)
            VALUES (:id, :app_code, :name, :url, :ref)
            """,
            {
                "id": client_id,
                "app_code": args.app_code,
                "name": args.name,
                "url": args.webhook_url,
                "ref": args.webhook_secret_ref,
            },
        )
        await _audit(conn, args, "CLIENT", client_id, "create_client", {"app_code": args.app_code})
    print(f"client {args.app_code} created: {client_id}")


async def set_client_status(args: argparse.Namespace) -> None:
    async with transaction() as conn:
        client_id = await _client_id(conn, args.app_code)
        await execute(
            conn,
            "UPDATE payment_clients SET status = :s, updated_at = now() WHERE id = :id",
            {"s": args.status, "id": client_id},
        )
        await _audit(conn, args, "CLIENT", client_id, "set_status", {"status": args.status})
    print(f"client {args.app_code} is now {args.status}")
    await _wait_for_cache(args)


async def set_webhook(args: argparse.Namespace) -> None:
    _validate_webhook_url(args.url, args.allow_insecure)
    async with transaction() as conn:
        client_id = await _client_id(conn, args.app_code)
        await execute(
            conn,
            "UPDATE payment_clients SET webhook_url = :u, webhook_secret_ref = :r, "
            "updated_at = now() WHERE id = :id",
            {"u": args.url, "r": args.secret_ref, "id": client_id},
        )
        await _audit(
            conn, args, "CLIENT", client_id, "set_webhook", {"secret_ref": args.secret_ref}
        )
    print("webhook endpoint updated (the secret itself lives in the environment variable)")


async def add_credential(args: argparse.Namespace) -> None:
    scopes = args.scopes or list(ALL_SCOPES)
    secret = generate_client_secret()
    oauth_client_id = f"cli_{uuid7().hex[:24]}"
    expires = datetime.fromisoformat(args.expires_at) if args.expires_at else None
    async with transaction() as conn:
        client_id = await _client_id(conn, args.app_code)
        credential_id = uuid7()
        await execute(
            conn,
            """
            INSERT INTO client_credentials
              (id, client_id, oauth_client_id, secret_hash, scopes, expires_at)
            VALUES (:id, :client_id, :oauth, :hash, :scopes, :expires)
            """,
            {
                "id": credential_id,
                "client_id": client_id,
                "oauth": oauth_client_id,
                "hash": hash_client_secret(secret),
                "scopes": scopes,
                "expires": expires,
            },
        )
        await _audit(conn, args, "CREDENTIAL", credential_id, "add_credential", {"scopes": scopes})
    print(f"oauth_client_id: {oauth_client_id}")
    print(f"client_secret:   {secret}")
    print("Store the secret now: only its hash is kept.")


async def revoke_credential(args: argparse.Namespace) -> None:
    async with transaction() as conn:
        row = await fetch_one(
            conn,
            "UPDATE client_credentials SET status = 'REVOKED', revoked_at = now() "
            "WHERE oauth_client_id = :id RETURNING id",
            {"id": args.oauth_client_id},
        )
        if row is None:
            raise SystemExit("unknown oauth_client_id")
        await _audit(conn, args, "CREDENTIAL", row["id"], "revoke_credential")
    print("credential revoked")
    await _wait_for_cache(args)


async def create_merchant(args: argparse.Namespace) -> None:
    async with transaction() as conn:
        merchant_id = uuid7()
        await execute(
            conn,
            """
            INSERT INTO merchant_accounts
              (id, merchant_code, environment, credential_ref, partner_id,
               provider_merchant_id, merchant_name, callback_config_ref)
            VALUES (:id, :code, :env, :ref, :partner, :mid, :name, :cb)
            """,
            {
                "id": merchant_id,
                "code": args.code,
                "env": args.environment,
                "ref": args.credential_ref,
                "partner": args.partner_id,
                "mid": args.provider_merchant_id,
                "name": args.merchant_name,
                "cb": args.callback_note,
            },
        )
        await _audit(
            conn, args, "MERCHANT_ACCOUNT", merchant_id, "create_merchant",
            {"code": args.code, "credential_ref": args.credential_ref},
        )  # fmt: skip
    print(f"merchant {args.code} created: {merchant_id}")
    print(
        f"set {args.credential_ref}_PRIVATE_KEY / _FASPAY_PUBLIC_KEY (SNAP) and "
        f"{args.credential_ref}_LEGACY_USER / _LEGACY_PASSWORD (Legacy) in the environment"
    )


async def set_merchant_status(args: argparse.Namespace) -> None:
    async with transaction() as conn:
        row = await fetch_one(
            conn,
            "UPDATE merchant_accounts SET status = :s, updated_at = now() "
            "WHERE merchant_code = :c RETURNING id",
            {"s": args.status, "c": args.code},
        )
        if row is None:
            raise SystemExit("unknown merchant")
        await _audit(
            conn, args, "MERCHANT_ACCOUNT", row["id"], "set_status", {"status": args.status}
        )
    await _wait_for_cache(args)


async def add_route(args: argparse.Namespace) -> None:
    async with transaction() as conn:
        client_id = await _client_id(conn, args.app_code)
        merchant = await fetch_one(
            conn,
            "SELECT id FROM merchant_accounts WHERE merchant_code = :c",
            {"c": args.merchant_code},
        )
        channel = await fetch_one(
            conn,
            "SELECT id FROM payment_channels WHERE payment_method = :m AND channel_code = :c "
            "AND protocol = :p",
            {"m": args.method, "c": args.channel_code, "p": args.protocol},
        )
        if merchant is None or channel is None:
            raise SystemExit("unknown merchant or channel (check --protocol)")
        route_id = uuid7()
        await execute(
            conn,
            """
            INSERT INTO client_merchant_routes
              (id, client_id, merchant_account_id, payment_channel_id, payment_method,
               channel_code, route_code, priority, is_default)
            VALUES (:id, :client, :merchant, :channel, :m, :c, :route, :priority, :default)
            """,
            {
                "id": route_id,
                "client": client_id,
                "merchant": merchant["id"],
                "channel": channel["id"],
                "m": args.method,
                "c": args.channel_code,
                "route": args.route_code,
                "priority": args.priority,
                "default": args.default,
            },
        )
        await _audit(
            conn, args, "ROUTE", route_id, "add_route",
            {"app_code": args.app_code, "merchant": args.merchant_code, "protocol": args.protocol,
             "method": args.method, "channel_code": args.channel_code},
        )  # fmt: skip
    print(f"route {route_id} added")
    await _wait_for_cache(args)


async def set_route_status(args: argparse.Namespace) -> None:
    async with transaction() as conn:
        row = await fetch_one(
            conn,
            "UPDATE client_merchant_routes SET status = :s, updated_at = now() "
            "WHERE id = :id RETURNING id",
            {"s": args.status, "id": args.route_id},
        )
        if row is None:
            raise SystemExit("unknown route")
        await _audit(conn, args, "ROUTE", row["id"], "set_status", {"status": args.status})
    await _wait_for_cache(args)


async def list_routes(args: argparse.Namespace) -> None:
    async with transaction() as conn:
        rows = await fetch_all(
            conn,
            """
            SELECT r.id, c.app_code, r.route_code, r.payment_method, r.channel_code, ch.protocol,
                   m.merchant_code, r.is_default, r.status
            FROM client_merchant_routes r
            JOIN payment_clients c ON c.id = r.client_id
            JOIN payment_channels ch ON ch.id = r.payment_channel_id
            JOIN merchant_accounts m ON m.id = r.merchant_account_id
            WHERE (CAST(:app AS VARCHAR) IS NULL OR c.app_code = :app)
            ORDER BY c.app_code, r.payment_method, r.channel_code
            """,
            {"app": args.app_code},
        )
    for r in rows:
        print(
            f"{r['id']}  {r['app_code']:<18} {r['route_code']:<10} {r['payment_method']:<8}"
            f" {r['channel_code']:<5} {r['protocol']:<6} {r['merchant_code']:<14}"
            f" default={r['is_default']} {r['status']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts.admin", description=__doc__)
    parser.add_argument("--operator", default=getpass.getuser(), help="recorded in audit_logs")
    parser.add_argument("--no-wait", action="store_true", help="do not wait for the cache TTL")
    parser.add_argument("--allow-insecure", action="store_true", help="allow http webhook URLs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, fn: Command) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        return p

    cmd("seed-channels", seed_channels)

    p = cmd("add-channel", add_channel)
    p.add_argument("--method", required=True, choices=["VA", "QRIS", "EWALLET", "RETAIL"])
    p.add_argument("--channel-code", required=True)
    p.add_argument("--protocol", required=True, choices=["SNAP", "LEGACY"])
    p.add_argument("--display-name", required=True)
    p.add_argument("--category")

    p = cmd("set-channel-status", set_channel_status)
    p.add_argument("--method", required=True)
    p.add_argument("--channel-code", required=True)
    p.add_argument("--protocol", required=True)
    p.add_argument("--status", required=True, choices=["ACTIVE", "INACTIVE"])

    p = cmd("create-client", create_client)
    p.add_argument("--app-code", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--webhook-url")
    p.add_argument("--webhook-secret-ref", help="env var NAME holding the signing secret")

    p = cmd("set-client-status", set_client_status)
    p.add_argument("--app-code", required=True)
    p.add_argument("--status", required=True, choices=["ACTIVE", "INACTIVE"])

    p = cmd("set-webhook", set_webhook)
    p.add_argument("--app-code", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--secret-ref", required=True)

    p = cmd("add-credential", add_credential)
    p.add_argument("--app-code", required=True)
    p.add_argument("--scopes", nargs="*", choices=list(ALL_SCOPES))
    p.add_argument("--expires-at", help="ISO-8601 with offset")

    p = cmd("revoke-credential", revoke_credential)
    p.add_argument("--oauth-client-id", required=True)

    p = cmd("create-merchant", create_merchant)
    p.add_argument("--code", required=True)
    p.add_argument("--environment", required=True, choices=["SANDBOX", "PRODUCTION"])
    p.add_argument("--credential-ref", required=True, help="env var PREFIX, e.g. KRN")
    p.add_argument("--provider-merchant-id", required=True)
    p.add_argument("--partner-id")
    p.add_argument("--merchant-name")
    p.add_argument("--callback-note")

    p = cmd("set-merchant-status", set_merchant_status)
    p.add_argument("--code", required=True)
    p.add_argument("--status", required=True, choices=["ACTIVE", "INACTIVE", "MAINTENANCE"])

    p = cmd("add-route", add_route)
    p.add_argument("--app-code", required=True)
    p.add_argument("--merchant-code", required=True)
    p.add_argument("--method", required=True, choices=["VA", "QRIS", "EWALLET", "RETAIL"])
    p.add_argument("--channel-code", required=True)
    p.add_argument("--protocol", required=True, choices=["SNAP", "LEGACY"])
    p.add_argument("--route-code", default="DEFAULT")
    p.add_argument("--priority", type=int, default=100)
    p.add_argument("--default", action="store_true")

    p = cmd("set-route-status", set_route_status)
    p.add_argument("--route-id", required=True)
    p.add_argument("--status", required=True, choices=["ACTIVE", "INACTIVE"])

    p = cmd("list-routes", list_routes)
    p.add_argument("--app-code")
    return parser


async def _run(args: argparse.Namespace) -> None:
    try:
        await args.fn(args)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main(sys.argv[1:])
