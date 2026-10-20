"""Pure unit tests: no database, no network."""

from __future__ import annotations

import base64
import hashlib
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from app.adapters.faspay.credentials import load_private_key, load_public_key
from app.adapters.faspay.legacy.mappers import amount_to_legacy, legacy_to_rupiah
from app.adapters.faspay.legacy.signing import legacy_notification_signature, legacy_signature
from app.adapters.faspay.snap import signing
from app.adapters.faspay.snap.mappers import amount_to_snap, deep_merge, snap_to_rupiah
from app.adapters.faspay.snap.verify import (
    NotificationFormatError,
    make_fingerprint,
    parse_notification,
)
from app.adapters.faspay.status_map import map_legacy_status, map_snap_status
from app.config import ConfigError, get_settings
from app.database import build_engine_args
from app.domain import FINAL_STATUSES, PaymentStatus
from app.services import tokens
from app.services.qstash import verify_signature
from app.services.retry import compute_backoff
from app.services.security import hash_client_secret, verify_admin_token, verify_client_secret
from app.services.state_machine import ALLOWED, can_transition, is_contradiction
from app.utils.hashing import b64url_sha256, sha256_hex
from app.utils.ids import new_partner_reference, uuid7
from app.utils.redact import redact_payload
from app.utils.timeutil import iso_jakarta, legacy_datetime, parse_iso
from app.utils.ttlcache import TTLCache
from tests.helpers import qstash_headers, va_notification
from tests.keys import FASPAY_PUBLIC_PEM, MERCHANT_PRIVATE_B64, MERCHANT_PUBLIC_PEM

S = PaymentStatus


# ── SNAP signing ─────────────────────────────────────────────────────────


def test_string_to_sign_follows_the_documented_formula():
    body = b'{"a":1}'
    expected = (
        "POST:/v1.0/qr/qr-mpm-generate:"
        + hashlib.sha256(body).hexdigest()
        + ":2026-10-20T09:30:00+07:00"
    )
    assert (
        signing.string_to_sign(
            "post", "/v1.0/qr/qr-mpm-generate", body, "2026-10-20T09:30:00+07:00"
        )
        == expected
    )
    assert hashlib.sha256(body).hexdigest() == hashlib.sha256(body).hexdigest().lower()


def test_sign_and_verify_roundtrip_with_independent_verification():
    key = load_private_key(MERCHANT_PRIVATE_B64)
    message = signing.string_to_sign("POST", "/p", b"{}", "2026-10-20T09:30:00+07:00")
    signature = signing.sign(key, message)
    # Independent verification straight through `cryptography`.
    key.public_key().verify(
        base64.b64decode(signature), message.encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    assert signing.verify(key.public_key(), signature, message)
    assert not signing.verify(key.public_key(), signature, message + "x")
    assert not signing.verify(key.public_key(), "not base64!!", message)
    assert not signing.verify(load_public_key(FASPAY_PUBLIC_PEM), signature, message)


def test_key_loading_accepts_pem_and_bare_base64_bodies():
    bare = load_private_key(MERCHANT_PRIVATE_B64)
    pem = bare.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    assert load_private_key(pem.replace("\n", "\\n")).private_numbers() == bare.private_numbers()
    spki_body = "".join(MERCHANT_PUBLIC_PEM.splitlines()[1:-1])
    assert load_public_key(spki_body).public_numbers() == bare.public_key().public_numbers()
    with pytest.raises(ConfigError):
        load_private_key("not a key")
    with pytest.raises(ConfigError):
        load_public_key("-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----")


def test_amount_conversions_are_exact():
    assert amount_to_snap(150000) == "150000.00"
    assert snap_to_rupiah("12000.00") == 12000
    assert snap_to_rupiah("12000") == 12000
    for bad in ("12000.50", "-1", "abc", "nan", "inf", ""):
        with pytest.raises(ValueError):
            snap_to_rupiah(bad)
    assert amount_to_legacy(150000) == "15000000"
    assert legacy_to_rupiah("1000") == 10  # the sample "1000" means Rp 10
    assert legacy_to_rupiah("15000000") == 150000
    with pytest.raises(ValueError):
        legacy_to_rupiah("1050")


def test_legacy_signature_is_sha1_of_md5():
    inner = hashlib.md5(b"userpwbill1").hexdigest()  # noqa: S324
    assert legacy_signature("user", "pw", "bill1") == hashlib.sha1(inner.encode()).hexdigest()  # noqa: S324
    assert legacy_signature("user", "pw", "bill1") != legacy_signature("user", "pw", "bill2")
    assert legacy_notification_signature("user", "pw", "bill1", "2") != legacy_signature(
        "user", "pw", "bill1"
    )


def test_deep_merge_overrides_without_mutating():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    merged = deep_merge(base, {"a": {"b": 9}, "e": 5})
    assert merged == {"a": {"b": 9, "c": 2}, "d": 3, "e": 5} and base["a"]["b"] == 1


# ── State machine and status tables ──────────────────────────────────────


def test_state_machine_is_exactly_the_normative_table():
    assert ALLOWED[S.CREATED] == {S.PENDING, S.FAILED, S.UNKNOWN}
    assert ALLOWED[S.PENDING] == {S.SUCCEEDED, S.FAILED, S.EXPIRED, S.CANCELLED, S.UNKNOWN}
    assert ALLOWED[S.UNKNOWN] == {S.PENDING, S.SUCCEEDED, S.FAILED, S.EXPIRED, S.CANCELLED}
    for final in FINAL_STATUSES:
        assert ALLOWED[final] == frozenset()
        for target in S:
            assert not can_transition(final, target)
    assert not can_transition(S.CREATED, S.SUCCEEDED)  # must go through PENDING/UNKNOWN
    assert is_contradiction(S.EXPIRED, S.SUCCEEDED)
    assert not is_contradiction(S.SUCCEEDED, S.SUCCEEDED)
    assert not is_contradiction(S.PENDING, S.SUCCEEDED)


def test_snap_status_table():
    expected = {
        "00": S.SUCCEEDED, "01": S.PENDING, "02": S.PENDING, "03": S.PENDING,
        "05": S.CANCELLED, "06": S.FAILED, "07": S.EXPIRED,
    }  # fmt: skip
    for raw, status in expected.items():
        assert map_snap_status(raw).status == status
    refund = map_snap_status("04")
    assert refund.status is None and refund.alert == "refund_review"
    unknown = map_snap_status("42")
    assert unknown.status is None and unknown.alert == "unmapped_provider_status"
    assert map_snap_status(None).status is None


def test_legacy_status_table():
    expected = {
        "2": S.SUCCEEDED, "0": S.PENDING, "1": S.PENDING, "3": S.FAILED,
        "7": S.EXPIRED, "8": S.CANCELLED,
    }  # fmt: skip
    for raw, status in expected.items():
        assert map_legacy_status(raw).status == status
    assert map_legacy_status(2).status == S.SUCCEEDED  # numeric input
    for raw in ("5", "9"):
        mapping = map_legacy_status(raw)
        assert mapping.status is None and mapping.enqueue_inquiry
    assert map_legacy_status("4").alert == "reversal_review"


def test_status_tables_are_overridable_by_configuration():
    settings = get_settings()
    settings.SNAP_STATUS_MAP_OVERRIDES = {"07": "RECORD", "01": "FAILED"}
    assert map_snap_status("07").status is None
    assert map_snap_status("01").status == S.FAILED
    assert map_snap_status("00").status == S.SUCCEEDED  # untouched
    settings.LEGACY_STATUS_MAP_OVERRIDES = {"5": "EXPIRED"}
    assert map_legacy_status("5").status == S.EXPIRED


# ── Retry, cache, ids, time ──────────────────────────────────────────────


def test_backoff_doubles_caps_and_jitters_within_bounds():
    kwargs = {"base": 60, "cap": 21600, "jitter_fraction": 0.2}
    no_jitter = [compute_backoff(n, rng=lambda: 0.0, **kwargs) for n in range(1, 12)]
    assert no_jitter[:6] == [60, 120, 240, 480, 960, 1920]
    assert max(no_jitter) == 21600  # capped
    for attempts in (1, 4, 20):
        base = compute_backoff(attempts, rng=lambda: 0.0, **kwargs)
        assert compute_backoff(attempts, rng=lambda: 1.0, **kwargs) == pytest.approx(base * 1.2)
        for _ in range(50):
            assert base <= compute_backoff(attempts, **kwargs) <= base * 1.2


def test_ttl_cache_expires_and_bounds_its_size():
    now = [0.0]
    cache: TTLCache[str, int] = TTLCache(30, maxsize=3, clock=lambda: now[0])
    cache.set("a", 1)
    assert cache.get("a") == 1
    now[0] = 29.9
    assert cache.get("a") == 1
    now[0] = 30.1
    assert cache.get("a") is None
    for i in range(10):
        cache.set(f"k{i}", i)
    assert len(cache._data) <= 3


def test_ids():
    a, b = uuid7(), uuid7()
    assert a.version == 7 and b.version == 7 and a != b
    time.sleep(0.003)
    assert uuid7() > a  # time ordered
    refs = {new_partner_reference() for _ in range(500)}
    assert len(refs) == 500
    assert all(len(r) == 16 and r.isalnum() and r.isupper() or r.isdigit() for r in refs)


def test_time_helpers():
    from datetime import UTC, datetime

    moment = datetime(2026, 10, 20, 2, 30, tzinfo=UTC)
    assert iso_jakarta(moment) == "2026-10-20T09:30:00+07:00"
    assert legacy_datetime(moment) == "2026-10-20 09:30:00"
    assert iso_jakarta(None) is None
    assert parse_iso("2026-10-20T09:30:00+07:00") == moment
    assert parse_iso("2026-10-20T02:30:00Z") == moment
    with pytest.raises(ValueError):
        parse_iso("2026-10-20T09:30:00")


def test_redaction_removes_personal_data_and_secrets():
    out = redact_payload(
        {
            "virtualAccountName": "Budi",
            "email": "b@x.id",
            "signature": "abc",
            "keep": {"customerName": "Ani", "amount": 5, "list": [{"msisdn": "081"}]},
        }
    )
    assert out == {
        "virtualAccountName": "[REDACTED]",
        "email": "[REDACTED]",
        "signature": "[REDACTED]",
        "keep": {"customerName": "[REDACTED]", "amount": 5, "list": [{"msisdn": "[REDACTED]"}]},
    }


# ── Notification parsing ─────────────────────────────────────────────────


def test_va_notification_parsing_and_fingerprint_stability():
    parsed = parse_notification(va_notification("TRX0000000000001"))
    assert parsed.shape == "VA" and parsed.merchant_id == "12345"
    assert parsed.partner_reference == "TRX0000000000001"
    assert parsed.provider_reference == "8888800000001"  # padding stripped
    assert parsed.amount == 150000 and parsed.currency == "IDR" and parsed.raw_status == "00"
    assert parsed.paid_at is not None and parsed.paid_at.utcoffset().total_seconds() == 7 * 3600
    # Fields that change on a retry (timestamps, names) do not change the fingerprint.
    retry = va_notification("TRX0000000000001")
    retry["additionalInfo"]["paymentDate"] = "2026-10-20 09:40:00"
    retry["virtualAccountName"] = "Different"
    assert make_fingerprint("m1", parsed) == make_fingerprint("m1", parse_notification(retry))
    other_status = parse_notification(va_notification("TRX0000000000001", status="07"))
    assert make_fingerprint("m1", parsed) != make_fingerprint("m1", other_status)
    assert make_fingerprint("m1", parsed) != make_fingerprint("m2", parsed)  # merchant scoped


def test_reference_shape_reads_merchant_from_either_location():
    from tests.helpers import reference_notification

    top = reference_notification("PARTNER1", merchant_id="12345")
    top["merchantId"] = top["additionalInfo"].pop("merchantId")  # e-wallet places it at top level
    assert parse_notification(top).merchant_id == "12345"
    assert parse_notification(reference_notification("PARTNER1")).shape == "REFERENCE"
    no_amount = reference_notification("PARTNER1", amount=None)
    assert parse_notification(no_amount).amount is None  # optional in this shape


@pytest.mark.parametrize(
    ("mutate", "case"),
    [
        (lambda p: p.pop("trxId"), "02"),
        (lambda p: p["additionalInfo"].pop("merchantId"), "02"),
        (lambda p: p.update(trxId="  "), "02"),
        (lambda p: p.update(trxId={"x": 1}), "01"),
        (lambda p: p.update(paidAmount={"value": "1.5", "currency": "IDR"}), "01"),
        (lambda p: p.update(paidAmount="150000"), "01"),
    ],
)
def test_malformed_va_notifications(mutate, case):
    payload = va_notification("TRX0000000000001")
    mutate(payload)
    with pytest.raises(NotificationFormatError) as exc:
        parse_notification(payload)
    assert exc.value.case == case


def test_unrecognized_payloads():
    for payload in ({}, {"hello": 1}, [], "x", None):
        with pytest.raises(NotificationFormatError):
            parse_notification(payload)


# ── Access tokens ────────────────────────────────────────────────────────


def _hand_made_hs256(claims: dict, secret: bytes) -> str:
    """PyJWT refuses to build this token, so craft it manually (the classic confusion attack)."""
    import hmac as _hmac
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    payload = b64(json.dumps(claims).encode())
    signature = _hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64(signature)}"


def _ids():
    return uuid.uuid4(), uuid.uuid4()


def test_hs256_tokens_roundtrip_in_development():
    tokens.reset_token_keys()
    client_id, cred_id = _ids()
    token, ttl = tokens.issue_access_token(
        client_id=client_id, credential_id=cred_id, scopes=["payments:read"]
    )
    claims = tokens.decode_access_token(token)
    assert ttl == 600 and claims["sub"] == str(client_id) and claims["cred"] == str(cred_id)
    assert claims["scope"] == "payments:read" and claims["kid"] == "k1" and claims["jti"]
    assert claims["exp"] - claims["iat"] == 600


def test_hs256_is_refused_in_production():
    tokens.reset_token_keys()
    settings = get_settings()
    settings.APP_ENV = "production"
    with pytest.raises(ConfigError):
        tokens.issue_access_token(client_id=uuid.uuid4(), credential_id=uuid.uuid4(), scopes=[])
    tokens.reset_token_keys()


def _pem_pair(kind: str):
    if kind == "rsa":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


@pytest.mark.parametrize(("kind", "alg"), [("rsa", "RS256"), ("ec", "ES256")])
def test_asymmetric_tokens_and_key_rotation(kind, alg):
    settings = get_settings()
    old_private, old_public = _pem_pair(kind)
    new_private, new_public = _pem_pair(kind)
    settings.OAUTH_JWT_SECRET = ""
    settings.APP_ENV = "production"

    settings.OAUTH_JWT_PRIVATE_KEY, settings.OAUTH_JWT_KID = old_private, "k-old"
    tokens.reset_token_keys()
    client_id, cred_id = _ids()
    old_token, _ = tokens.issue_access_token(
        client_id=client_id, credential_id=cred_id, scopes=["a"]
    )
    assert jwt.get_unverified_header(old_token) == {"alg": alg, "typ": "JWT", "kid": "k-old"}

    # Rotate: new signing key, old public key kept so live tokens keep working.
    settings.OAUTH_JWT_PRIVATE_KEY, settings.OAUTH_JWT_KID = new_private, "k-new"
    settings.OAUTH_JWT_PUBLIC_KEY_PREV, settings.OAUTH_JWT_KID_PREV = old_public, "k-old"
    tokens.reset_token_keys()
    assert tokens.decode_access_token(old_token)["sub"] == str(client_id)
    new_token, _ = tokens.issue_access_token(
        client_id=client_id, credential_id=cred_id, scopes=["a"]
    )
    assert tokens.decode_access_token(new_token)["sub"] == str(client_id)

    # Once the overlap ends, the old key is rejected.
    settings.OAUTH_JWT_PUBLIC_KEY_PREV = settings.OAUTH_JWT_KID_PREV = ""
    tokens.reset_token_keys()
    with pytest.raises(jwt.PyJWTError):
        tokens.decode_access_token(old_token)
    assert new_public  # (kept to document the pair)
    tokens.reset_token_keys()


def test_tokens_reject_forgery_expiry_and_algorithm_confusion():
    settings = get_settings()
    private, public = _pem_pair("rsa")
    settings.OAUTH_JWT_SECRET = ""
    settings.OAUTH_JWT_PRIVATE_KEY = private
    settings.APP_ENV = "production"
    tokens.reset_token_keys()
    client_id, cred_id = _ids()
    good, _ = tokens.issue_access_token(client_id=client_id, credential_id=cred_id, scopes=["a"])
    base = {
        "iss": settings.OAUTH_ISSUER, "aud": settings.OAUTH_AUDIENCE, "sub": str(client_id),
        "cred": str(cred_id), "iat": int(time.time()), "exp": int(time.time()) + 600,
        "scope": "a", "jti": "x",
    }  # fmt: skip

    # HS256 signed with the *public key as the secret* must not be accepted.
    confused = _hand_made_hs256(base, public.encode())
    unsigned = jwt.encode(base, None, algorithm="none", headers={"kid": "k1"})
    other_private, _ = _pem_pair("rsa")
    forged = jwt.encode(base, other_private, algorithm="RS256", headers={"kid": "k1"})
    expired = jwt.encode(
        {**base, "exp": int(time.time()) - 5}, private, "RS256", headers={"kid": "k1"}
    )
    wrong_aud = jwt.encode({**base, "aud": "other"}, private, "RS256", headers={"kid": "k1"})
    wrong_iss = jwt.encode({**base, "iss": "other"}, private, "RS256", headers={"kid": "k1"})
    no_kid = jwt.encode(base, private, algorithm="RS256")
    missing_claim = jwt.encode(
        {k: v for k, v in base.items() if k != "cred"}, private, "RS256", headers={"kid": "k1"}
    )
    for bad in (
        confused,
        unsigned,
        forged,
        expired,
        wrong_aud,
        wrong_iss,
        no_kid,
        missing_claim,
        "junk",
    ):
        with pytest.raises(jwt.PyJWTError):
            tokens.decode_access_token(bad)
    assert tokens.decode_access_token(good)["sub"] == str(client_id)
    tokens.reset_token_keys()


# ── Secrets, QStash, config ──────────────────────────────────────────────


def test_client_secret_hashing():
    stored = hash_client_secret("s3cret")
    assert stored.startswith("hmac-sha256$") and "s3cret" not in stored
    assert verify_client_secret("s3cret", stored)
    assert not verify_client_secret("other", stored)
    assert not verify_client_secret("s3cret", None)  # unknown client: constant-time dummy path
    get_settings().CLIENT_SECRET_PEPPER = "different"
    assert not verify_client_secret("s3cret", stored)  # the pepper is part of the hash
    get_settings().CLIENT_SECRET_PEPPER = ""
    with pytest.raises(ConfigError):
        hash_client_secret("x")


def test_admin_token_is_compared_against_its_hash():
    assert verify_admin_token("test-admin-token")
    assert not verify_admin_token("test-admin-token ")
    assert not verify_admin_token("")
    get_settings().ADMIN_API_TOKEN_HASH = ""
    assert not verify_admin_token("test-admin-token")  # unset hash disables the endpoints


def test_qstash_signature_verification():
    url, body = "https://gateway.test/internal/jobs/run", b'{"scan":true}'
    headers = qstash_headers(url, body)
    assert verify_signature(headers["Upstash-Signature"], body, url)
    assert not verify_signature(headers["Upstash-Signature"], body + b" ", url)
    assert not verify_signature(headers["Upstash-Signature"], body, url + "?x=1")
    assert not verify_signature(None, body, url)
    assert not verify_signature("", body, url)
    nxt = qstash_headers(url, body, key="qstash-next-key-for-tests-0123456789ab")
    assert verify_signature(nxt["Upstash-Signature"], body, url)
    assert not verify_signature(
        qstash_headers(url, body, key="x" * 40)["Upstash-Signature"], body, url
    )
    expired = jwt.encode(
        {
            "iss": "Upstash",
            "sub": url,
            "exp": int(time.time()) - 60,
            "nbf": 0,
            "body": b64url_sha256(body),
        },
        "qstash-current-key-for-tests-0123456789",
        algorithm="HS256",
    )
    assert not verify_signature(expired, body, url)
    wrong_issuer = jwt.encode(
        {
            "iss": "Someone",
            "sub": url,
            "exp": int(time.time()) + 60,
            "nbf": 0,
            "body": b64url_sha256(body),
        },
        "qstash-current-key-for-tests-0123456789",
        algorithm="HS256",
    )
    assert not verify_signature(wrong_issuer, body, url)
    assert sha256_hex("") == hashlib.sha256(b"").hexdigest()


def test_neon_urls_are_translated_for_asyncpg():
    url, args = build_engine_args(
        "postgresql://user:pw@ep-x-pooler.neon.tech/db?sslmode=require&channel_binding=require"
    )
    assert url.drivername == "postgresql+asyncpg" and url.query == {}
    assert args["statement_cache_size"] == 0 and args["ssl"] is True  # PgBouncer-safe
    assert args["prepared_statement_name_func"]() != args["prepared_statement_name_func"]()
    _, local = build_engine_args("postgresql+asyncpg://u@localhost/db")
    assert "ssl" not in local


def test_environment_mapping():
    settings = get_settings()
    settings.APP_ENV = "production"
    assert settings.environment == "PRODUCTION" and settings.is_production
    assert settings.faspay_base_url() == "https://web.faspay.co.id"
    settings.APP_ENV = "sandbox"
    assert settings.environment == "SANDBOX"
    assert settings.faspay_base_url() == "https://debit-sandbox.faspay.co.id"
