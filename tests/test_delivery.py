from __future__ import annotations

import hashlib
import hmac
import json
import socket

import httpx
import pytest

from app.config import get_settings
from app.services import delivery
from app.services.delivery import DestinationError, resolve_destination
from app.services.job_runner import run_jobs
from tests.helpers import (
    create,
    minified,
    payment_body,
    q,
    q1,
    run,
    sign_snap_callback,
    va_notification,
)

pytestmark = pytest.mark.integration

VA_PATH = "/v1.0/transfer-va/payment"


@pytest.fixture
async def paid(api, seed, auth_headers, faspay):
    """A SUCCEEDED payment with its DELIVER_WEBHOOK job pending."""
    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    headers = await auth_headers(api, seed.cf)
    created = (await create(api, headers, payment_body(merchant_reference="INV-PAID"))).json()
    ref = (await q1("SELECT provider_partner_reference AS r FROM payment_transactions"))["r"]
    body = minified(va_notification(ref))
    resp = await api.post(VA_PATH, content=body, headers=sign_snap_callback(VA_PATH, body))
    assert resp.status_code == 200
    return created


def expected_signature(secret: str, request: httpx.Request) -> str:
    timestamp = request.headers["x-gateway-timestamp"]
    return hmac.new(
        secret.encode(), timestamp.encode() + b"." + request.content, hashlib.sha256
    ).hexdigest()


async def test_signed_delivery_to_the_client_endpoint(paid, consumer):
    summary = await run_jobs()
    assert summary.done == 1
    (request,) = consumer.requests
    assert str(request.url) == "https://consumer.test/cf/hook"
    payload = json.loads(request.content)
    assert payload["event_type"] == "payment.succeeded"
    assert payload["event_id"].startswith("evt_")
    assert request.headers["x-gateway-event-id"] == payload["event_id"]
    assert request.headers["x-gateway-signature"] == "v1=" + expected_signature(
        "consumer-secret", request
    )
    assert abs(int(request.headers["x-gateway-timestamp"]) - __import__("time").time()) < 60
    p = payload["payment"]
    assert p["payment_id"] == paid["payment_id"]
    assert p["merchant_reference"] == "INV-PAID" and p["status"] == "SUCCEEDED"
    assert p["amount"] == 150000 and p["currency"] == "IDR" and p["payment_method"] == "VA"
    assert p["channel_code"] == "402" and p["paid_at"].endswith("+07:00")
    assert p["metadata"] == {"platform": "CROWDFUNDING", "program_id": "123"}
    assert payload["occurred_at"]
    row = await q1(
        "SELECT delivery_status, attempt_count, last_http_status FROM webhook_deliveries"
    )
    assert row == {"delivery_status": "DELIVERED", "attempt_count": 1, "last_http_status": 200}
    assert (await q1("SELECT status FROM gateway_jobs"))["status"] == "DONE"
    assert (await run_jobs()).claimed == 0  # nothing left; a re-run would not redeliver


async def test_retries_keep_the_same_event_id_then_succeed(paid, consumer):
    consumer.statuses = [500, 503]
    first = await run_jobs()
    assert first.retried == 1
    delivery_row = await q1("SELECT * FROM webhook_deliveries")
    assert delivery_row["delivery_status"] == "RETRY_SCHEDULED"
    assert delivery_row["last_http_status"] == 500 and delivery_row["next_retry_at"]
    assert delivery_row["last_error_category"] == "http_500"

    await run("UPDATE gateway_jobs SET run_after = now()")
    await run_jobs()
    await run("UPDATE gateway_jobs SET run_after = now()")
    last = await run_jobs()
    assert last.done == 1
    ids = {r.headers["x-gateway-event-id"] for r in consumer.requests}
    assert len(consumer.requests) == 3 and len(ids) == 1  # at-least-once, stable event id
    final = await q1("SELECT delivery_status, attempt_count FROM webhook_deliveries")
    assert final == {"delivery_status": "DELIVERED", "attempt_count": 3}


async def test_retry_after_is_honoured(paid, consumer):
    consumer.statuses = [429]

    def handler(request: httpx.Request) -> httpx.Response:
        consumer.requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "900"})

    delivery.set_delivery_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await run_jobs()
    wait = await q1("SELECT extract(epoch FROM run_after - now()) AS s FROM gateway_jobs")
    assert wait["s"] > 800  # waited at least the provider's Retry-After, beyond the 1 s backoff


async def test_client_error_is_dead_lettered_and_can_be_replayed(paid, consumer, api):
    consumer.statuses = [400]
    summary = await run_jobs()
    assert summary.dead == 1
    row = await q1("SELECT id, delivery_status, last_http_status FROM webhook_deliveries")
    assert row["delivery_status"] == "DEAD_LETTER"
    assert (await q1("SELECT status, last_error_category FROM gateway_jobs")) == {
        "status": "DEAD",
        "last_error_category": "http_400",
    }

    admin = {"Authorization": "Bearer test-admin-token", "X-Operator": "alice"}
    assert (await api.post(f"/internal/deliveries/{row['id']}/replay")).status_code == 401
    replay = await api.post(f"/internal/deliveries/{row['id']}/replay", headers=admin)
    assert replay.status_code == 202
    assert (await run_jobs()).done == 1  # the consumer is healthy again
    assert (await q1("SELECT delivery_status FROM webhook_deliveries"))[
        "delivery_status"
    ] == "DELIVERED"
    audit = await q1("SELECT actor_type, actor_id, action FROM audit_logs")
    assert audit == {"actor_type": "ADMIN", "actor_id": "alice", "action": "replay"}
    # A delivered one is not replayable.
    again = await api.post(f"/internal/deliveries/{row['id']}/replay", headers=admin)
    assert again.status_code == 409


async def test_exhausted_attempts_dead_letter(paid, consumer):
    get_settings().DELIVERY_MAX_ATTEMPTS = 2
    await run("UPDATE gateway_jobs SET max_attempts = 2")
    consumer.statuses = [500, 500]
    await run_jobs()
    await run("UPDATE gateway_jobs SET run_after = now()")
    summary = await run_jobs()
    assert summary.dead == 1
    assert (await q1("SELECT delivery_status FROM webhook_deliveries"))[
        "delivery_status"
    ] == "DEAD_LETTER"


async def test_secret_rotation_signs_with_both_secrets(paid, consumer, monkeypatch):
    monkeypatch.setenv("CF_WEBHOOK_SECRET_PREV", "old-secret")
    await run_jobs()
    (request,) = consumer.requests
    signatures = request.headers["x-gateway-signature"].split(",")
    assert signatures == [
        "v1=" + expected_signature("consumer-secret", request),
        "v1=" + expected_signature("old-secret", request),
    ]


async def test_missing_secret_retries_instead_of_losing_the_event(paid, consumer, monkeypatch):
    monkeypatch.delenv("CF_WEBHOOK_SECRET")
    summary = await run_jobs()
    assert summary.retried == 1 and consumer.requests == []
    assert (await q1("SELECT last_error_category FROM gateway_jobs"))[
        "last_error_category"
    ] == "secret_missing"
    monkeypatch.setenv("CF_WEBHOOK_SECRET", "consumer-secret")
    await run("UPDATE gateway_jobs SET run_after = now()")
    assert (await run_jobs()).done == 1


async def test_inactive_client_is_not_delivered_to(paid, consumer):
    await run("UPDATE payment_clients SET status='INACTIVE'")
    assert (await run_jobs()).dead == 1
    assert consumer.requests == []


async def test_destination_guard_applies_at_send_time(paid, consumer):
    get_settings().ALLOW_INSECURE_WEBHOOK_URLS = False
    summary = await run_jobs()
    assert summary.dead == 1  # the stored URL is https but resolves to a non-routable host
    assert consumer.requests == []
    row = await q1("SELECT delivery_status FROM webhook_deliveries")
    assert row["delivery_status"] == "DEAD_LETTER"


async def test_client_without_a_webhook_gets_no_delivery(api, seed, auth_headers, faspay):
    get_settings().CALLBACK_VERIFY_MODE = "SIGNATURE"
    await run("UPDATE payment_clients SET webhook_url = NULL")
    headers = await auth_headers(api, seed.cf)
    await create(api, headers, payment_body())
    ref = (await q1("SELECT provider_partner_reference AS r FROM payment_transactions"))["r"]
    body = minified(va_notification(ref))
    await api.post(VA_PATH, content=body, headers=sign_snap_callback(VA_PATH, body))
    assert await q("SELECT 1 FROM webhook_deliveries") == []
    assert (await q1("SELECT status FROM payment_transactions"))["status"] == "SUCCEEDED"


# ── SSRF guard (pure) ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
    ],
)
def test_blocked_addresses(ip):
    import ipaddress

    assert delivery._ip_blocked(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(ip):
    import ipaddress

    assert not delivery._ip_blocked(ipaddress.ip_address(ip))


async def test_destination_rules(monkeypatch):
    get_settings().ALLOW_INSECURE_WEBHOOK_URLS = False

    async def fake_getaddrinfo(self, host, port, **kwargs):
        table = {
            "public.example": ["93.184.216.34"],
            "internal.example": ["10.0.0.5"],
            "mixed.example": ["93.184.216.34", "127.0.0.1"],
        }
        if host not in table:
            raise socket.gaierror("nope")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", fake_getaddrinfo)

    for url, reason in (
        ("http://public.example/hook", "destination_not_https"),
        ("https://internal.example/hook", "destination_blocked"),
        ("https://mixed.example/hook", "destination_blocked"),  # every address must be public
        ("https://127.0.0.1/hook", "destination_blocked"),
        ("https://[::1]/hook", "destination_blocked"),
        ("https://169.254.169.254/latest/meta-data", "destination_blocked"),
        ("https://user:pw@public.example/hook", "destination_has_credentials"),
        ("https://missing.example/hook", "destination_unresolvable"),
        ("ftp://public.example/x", "destination_not_https"),
    ):
        with pytest.raises(DestinationError) as exc:
            await resolve_destination(url)
        assert str(exc.value) == reason, url

    ok = await resolve_destination("https://public.example:8443/a/b?x=1")
    # The request is pinned to the validated IP (no second DNS lookup), host kept for TLS/Host.
    assert ok.url == "https://93.184.216.34:8443/a/b?x=1"
    assert ok.host_header == "public.example:8443" and ok.sni_hostname == "public.example"
    plain = await resolve_destination("https://public.example/hook")
    assert plain.host_header == "public.example"


async def test_redirects_are_not_followed(db, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    delivery.set_delivery_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    destination = await resolve_destination("http://consumer.test/hook")  # insecure mode (tests)
    result = await delivery.send(destination, b"{}", {})
    assert result.status == 302 and calls == ["http://consumer.test/hook"]
