"""Shared test helpers: seed data, a fake Faspay, a fake consumer, signing utilities."""

from __future__ import annotations

import base64
import copy
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import text

from app.database import transaction
from app.repositories.db import execute, fetch_all, fetch_one
from app.services.security import hash_client_secret
from app.utils.hashing import b64url_sha256, sha256_hex
from app.utils.ids import uuid7
from app.utils.timeutil import snap_timestamp
from tests.keys import FASPAY_KEY, MERCHANT_KEY

# ── Seed ──────────────────────────────────────────────────────────────


@dataclass
class ClientSeed:
    id: uuid.UUID
    app_code: str
    oauth_client_id: str
    secret: str


@dataclass
class Seed:
    cf: ClientSeed
    fh: ClientSeed
    merchant_id: uuid.UUID
    prod_merchant_id: uuid.UUID
    channels: dict[str, uuid.UUID] = field(default_factory=dict)


async def _client(
    conn: Any, app_code: str, name: str, webhook_url: str, secret_ref: str
) -> ClientSeed:
    client_id = uuid7()
    await execute(
        conn,
        "INSERT INTO payment_clients (id, app_code, name, webhook_url, webhook_secret_ref) "
        "VALUES (:id, :app, :name, :url, :ref)",
        {"id": client_id, "app": app_code, "name": name, "url": webhook_url, "ref": secret_ref},
    )
    secret = f"secret-{app_code}-{uuid.uuid4().hex}"
    oauth_id = f"cli_{app_code.lower()}"
    await execute(
        conn,
        "INSERT INTO client_credentials (id, client_id, oauth_client_id, secret_hash, scopes) "
        "VALUES (:id, :client, :oauth, :hash, :scopes)",
        {
            "id": uuid7(),
            "client": client_id,
            "oauth": oauth_id,
            "hash": hash_client_secret(secret),
            "scopes": ["payments:write", "payments:read"],
        },
    )
    return ClientSeed(client_id, app_code, oauth_id, secret)


async def seed_world() -> Seed:
    async with transaction() as conn:
        cf = await _client(
            conn,
            "CROWDFUNDING",
            "Crowdfunding",
            "https://consumer.test/cf/hook",
            "CF_WEBHOOK_SECRET",
        )
        fh = await _client(
            conn, "FLUENCY_HUB", "Fluency Hub", "https://consumer.test/fh/hook", "FH_WEBHOOK_SECRET"
        )
        merchant_id, prod_id = uuid7(), uuid7()
        for mid, code, env, provider_id in (
            (merchant_id, "KRN-SBX", "SANDBOX", "12345"),
            (prod_id, "KRN-PRD", "PRODUCTION", "99999"),
        ):
            await execute(
                conn,
                "INSERT INTO merchant_accounts (id, merchant_code, environment, credential_ref, "
                "partner_id, provider_merchant_id, merchant_name) "
                "VALUES (:id, :code, :env, 'KRN', 'P1', :pid, 'Karina')",
                {"id": mid, "code": code, "env": env, "pid": provider_id},
            )
        seed = Seed(cf, fh, merchant_id, prod_id)
        for method, code, protocol in (
            ("VA", "402", "SNAP"),
            ("QRIS", "715", "SNAP"),
            ("EWALLET", "722", "SNAP"),
            ("RETAIL", "707", "LEGACY"),
        ):
            channel_id = uuid7()
            seed.channels[f"{method}:{code}"] = channel_id
            await execute(
                conn,
                "INSERT INTO payment_channels (id, payment_method, channel_code, protocol, "
                "display_name) VALUES (:id, :m, :c, :p, :n)",
                {"id": channel_id, "m": method, "c": code, "p": protocol, "n": f"{method} {code}"},
            )
            for client in (cf, fh):
                await execute(
                    conn,
                    "INSERT INTO client_merchant_routes (id, client_id, merchant_account_id, "
                    "payment_channel_id, payment_method, channel_code, route_code, is_default) "
                    "VALUES (:id, :client, :merchant, :channel, :m, :c, 'DEFAULT', true)",
                    {
                        "id": uuid7(),
                        "client": client.id,
                        "merchant": merchant_id,
                        "channel": channel_id,
                        "m": method,
                        "c": code,
                    },
                )
        # A route that points at a production merchant while this deployment is sandbox.
        await execute(
            conn,
            "INSERT INTO client_merchant_routes (id, client_id, merchant_account_id, "
            "payment_channel_id, payment_method, channel_code, route_code, is_default) "
            "VALUES (:id, :client, :merchant, :channel, 'QRIS', '715', 'PROD', false)",
            {
                "id": uuid7(),
                "client": cf.id,
                "merchant": prod_id,
                "channel": seed.channels["QRIS:715"],
            },
        )
    return seed


# ── DB shortcuts ──────────────────────────────────────────────────────


async def q(sql: str, **params: Any) -> list[dict[str, Any]]:
    async with transaction() as conn:
        return await fetch_all(conn, sql, params)


async def q1(sql: str, **params: Any) -> dict[str, Any] | None:
    async with transaction() as conn:
        return await fetch_one(conn, sql, params)


async def run(sql: str, **params: Any) -> None:
    async with transaction() as conn:
        await conn.execute(text(sql), params)


# ── Fake Faspay ───────────────────────────────────────────────────────

SNAP_OK = {
    "/v1.0/transfer-va/create-va": {
        "responseCode": "2002700",
        "responseMessage": "Successful",
        "virtualAccountData": {
            "partnerServiceId": "   88888",
            "customerNo": "00000001",
            "virtualAccountNo": "   8888800000001",
            "expiredDate": "2026-10-21T23:59:59+07:00",
            "additionalInfo": {"redirectUrl": "https://faspay.test/redirect/va"},
        },
    },
    "/v1.0/qr/qr-mpm-generate": {
        "responseCode": "2004700",
        "responseMessage": "Successful",
        "referenceNo": "QR-REF-1",
        "additionalInfo": {"qrImageUrl": "https://faspay.test/qr/1.png"},
    },
    "/v1.0/debit/payment-host-to-host": {
        "responseCode": "2005400",
        "responseMessage": "Successful",
        "referenceNo": "EW-REF-1",
        "webRedirectUrl": "https://faspay.test/wallet/1",
    },
    "/cvr/300011/10": {
        "response": "Post Data Transaction",
        "trx_id": "3004445550000123",
        "response_code": "00",
        "response_desc": "Sukses",
        "redirect_url": "https://faspay.test/retail/1",
    },
}


@dataclass
class Call:
    path: str
    body: dict[str, Any]
    headers: dict[str, str]
    signature_ok: bool | None


class FaspayMock:
    """Records requests, verifies SNAP signatures independently, returns canned replies."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._overrides: dict[str, Any] = {}

    def set(
        self,
        path: str,
        response: dict[str, Any] | None = None,
        *,
        status: int = 200,
        exc: Exception | None = None,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        self._overrides[path] = handler or (exc if exc else (status, response))

    def _default_reply(self, path: str) -> dict[str, Any]:
        """Faspay assigns a fresh reference per payment; mimic that."""
        n = len(self.calls_to(path))
        reply = copy.deepcopy(SNAP_OK[path])
        if path == "/v1.0/transfer-va/create-va":
            data = reply["virtualAccountData"]
            data["virtualAccountNo"] = f"   88888{n:08d}"
            data["customerNo"] = f"{n:08d}"
        elif path == "/v1.0/qr/qr-mpm-generate":
            reply["referenceNo"] = f"QR-REF-{n}"
        elif path == "/v1.0/debit/payment-host-to-host":
            reply["referenceNo"] = f"EW-REF-{n}"
        elif path == "/cvr/300011/10":
            reply["trx_id"] = f"30044455500{122 + n:05d}"
        return reply

    def calls_to(self, path: str) -> list[Call]:
        return [c for c in self.calls if c.path == path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        raw = request.content
        body = json.loads(raw) if raw else {}
        signature_ok: bool | None = None
        if path.startswith("/v1.0"):
            signature_ok = _verify_snap_request(request, raw)
        self.calls.append(Call(path, body, dict(request.headers), signature_ok))

        override = self._overrides.get(path)
        if isinstance(override, Exception):
            raise override
        if callable(override):
            return override(request)
        if override is not None:
            status, payload = override
            return httpx.Response(status, json=payload)
        if path in SNAP_OK:
            return httpx.Response(200, json=self._default_reply(path))
        return httpx.Response(404, json={"responseCode": "4040000", "responseMessage": "nf"})


def _verify_snap_request(request: httpx.Request, raw: bytes) -> bool:
    """Independent re-implementation of the documented signature (not the app's code)."""
    timestamp = request.headers["x-timestamp"]
    string_to_sign = f"POST:{request.url.path}:{sha256_hex(raw).lower()}:{timestamp}"
    try:
        MERCHANT_KEY.public_key().verify(
            base64.b64decode(request.headers["x-signature"]),
            string_to_sign.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception:  # noqa: BLE001
        return False
    return True


def snap_status_reply(code: str, *, va: bool = False, amount: int | None = None) -> dict[str, Any]:
    if va:
        data: dict[str, Any] = {"paymentFlagStatus": code, "virtualAccountNo": "8888800000001"}
        if amount is not None:
            data["paidAmount"] = {"value": f"{amount}.00", "currency": "IDR"}
        return {
            "responseCode": "2002600",
            "responseMessage": "Successful",
            "virtualAccountData": data,
        }
    reply: dict[str, Any] = {
        "responseCode": "2005100",
        "responseMessage": "Successful",
        "latestTransactionStatus": code,
    }
    if amount is not None:
        reply["amount"] = {"value": f"{amount}.00", "currency": "IDR"}
    return reply


def legacy_inquiry_reply(status_code: str = "2") -> dict[str, Any]:
    return {
        "response": "Inquiry Status Payment",
        "trx_id": "3004445550000123",
        "response_code": "00",
        "response_desc": "Sukses",
        "payment_status_code": status_code,
    }


# ── Fake consumer ─────────────────────────────────────────────────────


class ConsumerMock:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.statuses: list[int] = []  # popped per request; 200 when empty

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(status, json={"ok": status < 300})


# ── Callback construction ─────────────────────────────────────────────


def minified(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def sign_snap_callback(
    path: str, raw: bytes, *, key: Any = FASPAY_KEY, timestamp: str | None = None
) -> dict[str, str]:
    ts = timestamp or snap_timestamp()
    message = f"POST:{path}:{sha256_hex(raw).lower()}:{ts}"
    signature = key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    return {
        "X-TIMESTAMP": ts,
        "X-SIGNATURE_FROM_FASPAY": base64.b64encode(signature).decode(),
        "Content-Type": "application/json",
    }


def va_notification(
    trx_id: str,
    *,
    va_no: str = "8888800000001",
    amount: int | None = 150000,
    status: str = "00",
    merchant_id: str = "12345",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "partnerServiceId": "   88888",
        "customerNo": "00000001",
        "virtualAccountNo": f"   {va_no}",
        "virtualAccountName": "Contoh Pelanggan",
        "paymentRequestId": va_no,
        "trxId": trx_id,
        "additionalInfo": {
            "paymentDate": "2026-10-20 09:34:15",
            "channelCode": "402",
            "merchantId": merchant_id,
            "latestTransactionStatus": status,
        },
    }
    if amount is not None:
        payload["paidAmount"] = {"value": f"{amount}.00", "currency": "IDR"}
    return payload


def reference_notification(
    partner_reference: str,
    *,
    provider_reference: str = "QR-REF-1",
    amount: int | None = 150000,
    status: str = "00",
    merchant_id: str = "12345",
    channel: str = "715",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "originalPartnerReferenceNo": partner_reference,
        "originalReferenceNo": provider_reference,
        "latestTransactionStatus": status,
        "additionalInfo": {
            "paymentDate": "2026-10-20T09:34:15+07:00",
            "channelCode": channel,
            "merchantId": merchant_id,
        },
    }
    if amount is not None:
        payload["amount"] = {"value": f"{amount}.00", "currency": "IDR"}
    return payload


def legacy_notification(
    bill_no: str,
    *,
    trx_id: str = "3004445550000123",
    status: str = "2",
    total: str | None = "15000000",
    merchant_id: str = "12345",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request": "Payment Notification",
        "trx_id": trx_id,
        "merchant_id": merchant_id,
        "merchant": "Karina",
        "bill_no": bill_no,
        "payment_reff": "123",
        "payment_date": "2026-10-20 09:34:15",
        "payment_status_code": status,
        "payment_status_desc": "Payment Success",
        "payment_channel_uid": "707",
        "payment_channel": "Alfagroup",
        "signature": "ignored",
    }
    if total is not None:
        payload["bill_total"] = total
        payload["payment_total"] = total
    return payload


def qstash_headers(
    url: str, body: bytes, key: str = "qstash-current-key-for-tests-0123456789"
) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "Upstash",
            "sub": url,
            "exp": now + 300,
            "nbf": now - 5,
            "iat": now,
            "jti": uuid.uuid4().hex,
            "body": b64url_sha256(body),
        },
        key,
        algorithm="HS256",
    )
    return {"Upstash-Signature": token, "Content-Type": "application/json"}


# ── API shortcuts ─────────────────────────────────────────────────────


def payment_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "merchant_reference": f"INV-{uuid.uuid4().hex[:10]}",
        "amount": 150000,
        "currency": "IDR",
        "payment_method": "VA",
        "channel_code": "402",
        "description": "Donasi",
        "customer": {
            "name": "Contoh Pelanggan",
            "email": "customer@example.com",
            "phone": "081234567890",
        },
        "metadata": {"platform": "CROWDFUNDING", "program_id": "123"},
    }
    body.update(overrides)
    return body


async def create(
    api: httpx.AsyncClient,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    *,
    key: str | None = None,
) -> httpx.Response:
    return await api.post(
        "/v1/payments",
        json=body or payment_body(),
        headers={**headers, "Idempotency-Key": key or uuid.uuid4().hex},
    )
