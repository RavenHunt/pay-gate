from __future__ import annotations

import base64

import pytest

from tests.helpers import run

pytestmark = pytest.mark.integration


async def test_token_issued_with_basic_auth(api, seed):
    resp = await api.post(
        "/v1/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(seed.cf.oauth_client_id, seed.cf.secret),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 600
    assert set(body["scope"].split()) == {"payments:write", "payments:read"}
    assert resp.headers["cache-control"] == "no-store"


async def test_token_issued_with_form_credentials(api, seed):
    resp = await api.post(
        "/v1/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": seed.cf.oauth_client_id,
            "client_secret": seed.cf.secret,
            "scope": "payments:read",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["scope"] == "payments:read"


async def test_wrong_secret_is_invalid_client(api, seed):
    resp = await api.post(
        "/v1/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(seed.cf.oauth_client_id, "wrong"),
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"


async def test_unknown_client_is_invalid_client(api, seed):
    resp = await api.post(
        "/v1/oauth/token", data={"grant_type": "client_credentials"}, auth=("nobody", "x")
    )
    assert resp.status_code == 401


async def test_grant_type_errors(api, seed):
    auth = (seed.cf.oauth_client_id, seed.cf.secret)
    resp = await api.post("/v1/oauth/token", data={}, auth=auth)
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_request"
    resp = await api.post("/v1/oauth/token", data={"grant_type": "password"}, auth=auth)
    assert resp.status_code == 400 and resp.json()["error"] == "unsupported_grant_type"


async def test_scope_must_be_allowed(api, seed):
    await run("UPDATE client_credentials SET scopes = ARRAY['payments:read']")
    resp = await api.post(
        "/v1/oauth/token",
        data={"grant_type": "client_credentials", "scope": "payments:write"},
        auth=(seed.cf.oauth_client_id, seed.cf.secret),
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_scope"


async def test_inactive_client_and_revoked_credential_cannot_get_tokens(api, seed):
    auth = (seed.cf.oauth_client_id, seed.cf.secret)
    await run("UPDATE client_credentials SET status='REVOKED'")
    resp = await api.post("/v1/oauth/token", data={"grant_type": "client_credentials"}, auth=auth)
    assert resp.status_code == 401
    await run("UPDATE client_credentials SET status='ACTIVE'")
    await run("UPDATE payment_clients SET status='INACTIVE'")
    resp = await api.post("/v1/oauth/token", data={"grant_type": "client_credentials"}, auth=auth)
    assert resp.status_code == 401


async def test_repeated_failures_are_throttled(api, seed):
    for _ in range(10):
        await api.post(
            "/v1/oauth/token", data={"grant_type": "client_credentials"}, auth=("victim", "x")
        )
    resp = await api.post(
        "/v1/oauth/token", data={"grant_type": "client_credentials"}, auth=("victim", "x")
    )
    assert resp.status_code == 429


async def test_revocation_takes_effect_on_existing_token(api, seed, auth_headers):
    from app.dependencies import reset_auth_cache

    headers = await auth_headers(api, seed.cf)
    assert (await api.get("/v1/payments/by-reference/NOPE", headers=headers)).status_code == 404
    await run("UPDATE client_credentials SET status='REVOKED', revoked_at=now()")
    reset_auth_cache()  # the status cache TTL (30 s) is what bounds this in production
    assert (await api.get("/v1/payments/by-reference/NOPE", headers=headers)).status_code == 401


async def test_deactivated_client_gets_403(api, seed, auth_headers):
    from app.dependencies import reset_auth_cache

    headers = await auth_headers(api, seed.cf)
    await run("UPDATE payment_clients SET status='INACTIVE'")
    reset_auth_cache()
    resp = await api.get("/v1/payments/by-reference/NOPE", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CLIENT_INACTIVE"


async def test_bearer_required_and_garbage_rejected(api, seed):
    assert (await api.get("/v1/payments/by-reference/X")).status_code == 401
    resp = await api.get("/v1/payments/by-reference/X", headers={"Authorization": "Bearer junk"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
    assert base64  # silence unused import in some linters
