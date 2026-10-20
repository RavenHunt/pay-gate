"""Application configuration (pydantic-settings) and environment secret resolution.

Everything the Faspay documentation leaves open (TRD section 20) is a setting
here, so sandbox findings change configuration rather than code.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Credentials are resolved from os.environ by name at runtime, so make sure a
# local .env is visible there as well (Vercel injects real variables).
load_dotenv()


class ConfigError(RuntimeError):
    """A required environment variable is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── App ───────────────────────────────────────────────────────
    APP_NAME: str = "payment-gateway"
    APP_ENV: str = "development"  # development | test | sandbox | production
    DEBUG: bool = False
    LOG_LEVEL: str = "info"
    PUBLIC_BASE_URL: str = ""  # used for QStash targets, e.g. https://gateway.example.com
    CORS_ALLOW_ORIGINS: list[str] = Field(default_factory=list)

    # ── Database (Neon pooled URL) ────────────────────────────────
    DATABASE_URL: str = ""
    DB_ECHO: bool = False

    # ── OAuth (client credentials) ────────────────────────────────
    OAUTH_ISSUER: str = "payment-gateway"
    OAUTH_AUDIENCE: str = "payment-gateway-api"
    OAUTH_TOKEN_TTL_SECONDS: int = 600
    OAUTH_JWT_PRIVATE_KEY: str = ""  # RS256 / ES256 PEM
    OAUTH_JWT_PUBLIC_KEY: str = ""
    OAUTH_JWT_KID: str = "k1"
    OAUTH_JWT_PUBLIC_KEY_PREV: str = ""  # overlapping key during rotation
    OAUTH_JWT_KID_PREV: str = ""
    OAUTH_JWT_SECRET: str = ""  # HS256, local development only
    CLIENT_SECRET_PEPPER: str = ""  # server pepper for keyed HMAC-SHA-256 secret hashes
    OAUTH_FAILED_ATTEMPTS_PER_MINUTE: int = 10
    STATUS_CACHE_TTL_SECONDS: int = 30  # client, credential, merchant, route caches

    # ── Admin ─────────────────────────────────────────────────────
    ADMIN_API_TOKEN_HASH: str = ""  # SHA-256 hex of the admin bearer token

    # ── QStash ────────────────────────────────────────────────────
    QSTASH_TOKEN: str = ""
    QSTASH_URL: str = "https://qstash.upstash.io"
    QSTASH_CURRENT_SIGNING_KEY: str = ""
    QSTASH_NEXT_SIGNING_KEY: str = ""
    QSTASH_NUDGE_ENABLED: bool = True
    QSTASH_NUDGE_TIMEOUT_SECONDS: float = 2.0
    QSTASH_NUDGE_MIN_INTERVAL_SECONDS: float = 5.0

    # ── Payment limits and expiry (all to be agreed, see PRD 15) ──
    DEFAULT_CUSTOMER_NAME: str = "Customer"  # filled when the consumer sends no name
    PAYMENT_MIN_AMOUNT: int = 1
    PAYMENT_MAX_AMOUNT: int = 1_000_000_000
    METADATA_MAX_KEYS: int = 20
    METADATA_MAX_KEY_LENGTH: int = 64
    METADATA_MAX_VALUE_LENGTH: int = 256
    # Existing integrations: 5 hours for SNAP, 1 day for Legacy.
    EXPIRES_DEFAULT_MINUTES_BY_PROTOCOL: dict[str, int] = Field(
        default_factory=lambda: {"SNAP": 300, "LEGACY": 1440}
    )
    EXPIRES_MAX_MINUTES_BY_METHOD: dict[str, int] = Field(
        default_factory=lambda: {"VA": 10080, "QRIS": 1440, "EWALLET": 1440, "RETAIL": 10080}
    )
    EXPIRES_MIN_MINUTES: int = 5

    # ── Faspay (shared) ───────────────────────────────────────────
    FASPAY_PRODUCTION_BASE_URL: str = "https://web.faspay.co.id"
    FASPAY_SANDBOX_BASE_URL: str = "https://debit-sandbox.faspay.co.id"
    FASPAY_CREATE_TIMEOUT_SECONDS: float = 15.0
    FASPAY_INQUIRY_TIMEOUT_SECONDS: float = 8.0

    # ── SNAP driver ───────────────────────────────────────────────
    SNAP_CHANNEL_ID: str = "77001"
    # Open item: notification headers/path form. Comma separated, tried in order.
    SNAP_CALLBACK_SIGNATURE_HEADERS: str = "X-SIGNATURE_FROM_FASPAY,X-SIGNATURE"
    SNAP_CALLBACK_PATH_MODES: str = "path"  # path | path_query | full_url
    SNAP_CALLBACK_TIMESTAMP_TOLERANCE_SECONDS: int = 600
    SNAP_ALLOW_UNSIGNED_WITH_INQUIRY: bool = True
    CALLBACK_VERIFY_MODE: str = "SIGNATURE_AND_INQUIRY"  # or SIGNATURE
    SNAP_FASPAY_PUBLIC_KEY: str = ""  # shared fallback for {PREFIX}_FASPAY_PUBLIC_KEY
    SNAP_QRIS_QUERY_SERVICE_CODE: str = "51"  # docs say 51, legacy code sends 47
    SNAP_EWALLET_STATUS_SERVICE_CODE: str = "55"
    SNAP_DUPLICATE_ACK_MODE: str = "SUCCESS"  # SUCCESS | CONFLICT (documented 409SS01)
    SNAP_BODY_OVERRIDES: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Open item: status table. {"07": "EXPIRED"}; use "RECORD" to store without applying.
    SNAP_STATUS_MAP_OVERRIDES: dict[str, str] = Field(default_factory=dict)

    # ── Legacy driver ─────────────────────────────────────────────
    LEGACY_PAY_TYPE: str = "1"
    LEGACY_TERMINAL: str = "10"
    LEGACY_VERIFY_NOTIFICATION_SIGNATURE: bool = False  # formula unconfirmed
    LEGACY_REQUIRE_AMOUNT_FOR_SUCCESS: bool = True
    LEGACY_ACK_USE_INQUIRY_CODE: bool = True  # existing app echoes the inquiry code
    LEGACY_ERROR_RESPONSE_CODE: str = "99"
    LEGACY_BODY_OVERRIDES: dict[str, Any] = Field(default_factory=dict)
    LEGACY_STATUS_MAP_OVERRIDES: dict[str, str] = Field(default_factory=dict)

    # ── Webhook ingress ───────────────────────────────────────────
    REJECTED_EVENTS_PER_MINUTE: int = 120  # per instance; edge rate limits come first

    # ── Jobs, retry and reconciliation (TRD 12) ───────────────────
    FUNCTION_MAX_DURATION_SECONDS: int = 60
    JOBS_DEFAULT_MAX_JOBS: int = 25
    JOBS_MAX_JOBS_CAP: int = 100
    JOBS_DEFAULT_TIME_BUDGET_MS: int = 45_000
    JOBS_TIME_BUDGET_CAP_MS: int = 55_000
    JOBS_CLAIM_BATCH_SIZE: int = 5
    JOBS_LEASE_SECONDS: int = 90
    JOBS_STOP_CLAIMING_FRACTION: float = 0.7
    RETRY_BASE_SECONDS: int = 60
    RETRY_CAP_SECONDS: int = 21_600
    RETRY_JITTER_FRACTION: float = 0.2
    DELIVERY_MAX_ATTEMPTS: int = 8
    INQUIRY_MAX_ATTEMPTS: int = 8
    CORRELATION_MAX_ATTEMPTS: int = 8
    CORRELATION_RETRY_DELAY_SECONDS: int = 30
    UNKNOWN_INQUIRY_DELAY_SECONDS: int = 120
    CREATED_STALE_MINUTES: int = 5
    STALE_PENDING_MINUTES_BY_METHOD: dict[str, int] = Field(
        default_factory=lambda: {"QRIS": 15, "EWALLET": 15, "VA": 60, "RETAIL": 360}
    )
    EXPIRY_FORCE_AFTER_MINUTES: int = 0  # 0 = only inquiry-confirmed expiry
    SCAN_BATCH_LIMIT: int = 200

    # ── Consumer delivery ─────────────────────────────────────────
    DELIVERY_TIMEOUT_SECONDS: float = 10.0
    DELIVERY_MAX_RESPONSE_BYTES: int = 65_536
    ALLOW_INSECURE_WEBHOOK_URLS: bool = False  # development and tests only

    # ── Retention (ERD 6, to be agreed) ───────────────────────────
    RETENTION_WEBHOOK_EVENT_DAYS: int = 30
    RETENTION_JOB_DAYS: int = 30
    RETENTION_IDEMPOTENCY_DAYS: int = 30
    RETENTION_LONG_DAYS: int = 365
    PURGE_BATCH_LIMIT: int = 5000

    @property
    def environment(self) -> str:
        """Merchant-account environment this deployment serves."""
        return "PRODUCTION" if self.APP_ENV.lower() == "production" else "SANDBOX"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() == "production"

    def faspay_base_url(self) -> str:
        return (
            self.FASPAY_PRODUCTION_BASE_URL if self.is_production else self.FASPAY_SANDBOX_BASE_URL
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


def resolve_secret(name: str, *, required: bool = True) -> str:
    """Read a secret from the environment. Never log the returned value."""
    value = os.environ.get(name, "")
    if not value and required:
        raise ConfigError(f"Environment variable {name} is not set")
    return value
