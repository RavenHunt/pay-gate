from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

# Environment must be in place before the app (and its cached settings) is imported.
from tests.keys import FASPAY_PUBLIC_PEM, MERCHANT_PRIVATE_B64

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:54329/gateway_test"
)
ADMIN_TOKEN = "test-admin-token"
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DB_URL,
        "OAUTH_JWT_SECRET": "unit-test-jwt-secret-that-is-long-enough",
        "CLIENT_SECRET_PEPPER": "test-pepper",
        "ADMIN_API_TOKEN_HASH": hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest(),
        "QSTASH_CURRENT_SIGNING_KEY": "qstash-current-key-for-tests-0123456789",
        "QSTASH_NEXT_SIGNING_KEY": "qstash-next-key-for-tests-0123456789ab",
        "ALLOW_INSECURE_WEBHOOK_URLS": "true",
        "PUBLIC_BASE_URL": "https://gateway.test",
        "KRN_PRIVATE_KEY": MERCHANT_PRIVATE_B64,
        "KRN_FASPAY_PUBLIC_KEY": FASPAY_PUBLIC_PEM,
        "KRN_LEGACY_USER": "bot123",
        "KRN_LEGACY_PASSWORD": "pass456",
        "CF_WEBHOOK_SECRET": "consumer-secret",
        "FH_WEBHOOK_SECRET": "consumer-secret-fh",
        "LOG_LEVEL": "warning",
        "RETRY_BASE_SECONDS": "1",
        "UNKNOWN_INQUIRY_DELAY_SECONDS": "0",
        "CORRELATION_RETRY_DELAY_SECONDS": "0",
    }
)

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.adapters.faspay.http import set_http_client  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import configure_engine, dispose_engine, get_engine  # noqa: E402
from app.dependencies import reset_auth_cache  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.delivery import set_delivery_client  # noqa: E402
from app.services.event_processor import _reject_window  # noqa: E402
from app.services.merchant_lookup import reset_merchant_cache  # noqa: E402
from app.services.routing import reset_route_cache  # noqa: E402
from app.services.tokens import reset_token_keys  # noqa: E402
from tests.helpers import FaspayMock, Seed, seed_world  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs PostgreSQL")


def _db_available() -> bool:
    import asyncio

    import asyncpg

    async def probe() -> bool:
        try:
            conn = await asyncpg.connect(TEST_DB_URL.replace("postgresql+asyncpg", "postgresql"))
            await conn.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    return asyncio.run(probe())


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[None]:
    if not _db_available():
        pytest.skip(f"PostgreSQL not reachable at TEST_DATABASE_URL={TEST_DB_URL}")
    env = {**os.environ, "MIGRATION_DATABASE_URL": TEST_DB_URL}
    for command in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run(
            [sys.executable, "-m", "alembic", *command],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
    yield


@pytest_asyncio.fixture
async def db(migrated_db: None) -> AsyncIterator[None]:
    configure_engine(TEST_DB_URL)
    async with get_engine().begin() as conn:
        tables = (
            "audit_logs, api_idempotency_keys, gateway_jobs, webhook_deliveries, payment_events, "
            "webhook_events, payment_attempts, payment_transactions, client_merchant_routes, "
            "payment_channels, merchant_accounts, client_credentials, payment_clients"
        )
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    reset_auth_cache()
    reset_route_cache()
    reset_merchant_cache()
    reset_token_keys()
    _reject_window.clear()
    yield
    await dispose_engine()


@pytest.fixture
def faspay(monkeypatch: pytest.MonkeyPatch) -> Iterator[FaspayMock]:
    mock = FaspayMock()
    set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(mock.handle)))
    yield mock
    set_http_client(None)


@pytest.fixture(autouse=True)
def _no_real_network() -> Iterator[None]:
    """Any outbound call a test forgot to mock fails loudly instead of hitting the network."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unmocked outbound request: {request.method} {request.url}")

    set_delivery_client(httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    yield
    set_delivery_client(None)
    set_http_client(None)


@pytest.fixture
def consumer() -> Iterator[Any]:
    from tests.helpers import ConsumerMock

    mock = ConsumerMock()
    set_delivery_client(httpx.AsyncClient(transport=httpx.MockTransport(mock.handle)))
    yield mock
    set_delivery_client(None)


@pytest_asyncio.fixture
async def seed(db: None) -> Seed:
    return await seed_world()


@pytest_asyncio.fixture
async def api(seed: Seed, faspay: FaspayMock) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def auth_headers() -> Callable[..., Any]:
    async def _get(
        api: httpx.AsyncClient, client_seed: Any, scope: str | None = None
    ) -> dict[str, str]:
        data = {"grant_type": "client_credentials"}
        if scope:
            data["scope"] = scope
        resp = await api.post(
            "/v1/oauth/token",
            data=data,
            auth=(client_seed.oauth_client_id, client_seed.secret),
        )
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    return _get


@pytest.fixture(autouse=True)
def _settings_isolation() -> Iterator[None]:
    settings = get_settings()
    snapshot = settings.model_copy()
    yield
    for name in type(settings).model_fields:
        setattr(settings, name, getattr(snapshot, name))
