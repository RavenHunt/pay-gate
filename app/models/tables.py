"""Schema from ERD sections 3 and 5."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

TS = DateTime(timezone=True)
NOW = func.now()


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class PaymentClient(Base):
    __tablename__ = "payment_clients"
    __table_args__ = (CheckConstraint(_in("status", ("ACTIVE", "INACTIVE")), name="status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    app_code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), server_default="ACTIVE")
    webhook_url: Mapped[str | None] = mapped_column(Text)
    webhook_secret_ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)


class ClientCredential(Base):
    __tablename__ = "client_credentials"
    __table_args__ = (CheckConstraint(_in("status", ("ACTIVE", "REVOKED")), name="status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_clients.id")
    )
    oauth_client_id: Mapped[str] = mapped_column(String(64), unique=True)
    secret_hash: Mapped[str] = mapped_column(String(255))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    status: Mapped[str] = mapped_column(String(20), server_default="ACTIVE")
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    last_used_at: Mapped[datetime | None] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)


Index("ix_client_credentials_client_id", ClientCredential.client_id)


class MerchantAccount(Base):
    __tablename__ = "merchant_accounts"
    __table_args__ = (
        CheckConstraint(_in("environment", ("SANDBOX", "PRODUCTION")), name="environment"),
        CheckConstraint(_in("status", ("ACTIVE", "INACTIVE", "MAINTENANCE")), name="status"),
        Index(
            "ux_merchant_env_provider_merchant_id",
            "environment",
            "provider_merchant_id",
            unique=True,
            postgresql_where=text("provider_merchant_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    merchant_code: Mapped[str] = mapped_column(String(64), unique=True)
    provider: Mapped[str] = mapped_column(String(32), server_default="FASPAY")
    environment: Mapped[str] = mapped_column(String(16))
    credential_ref: Mapped[str] = mapped_column(String(64))
    partner_id: Mapped[str | None] = mapped_column(String(16))
    provider_merchant_id: Mapped[str | None] = mapped_column(String(64))
    merchant_name: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), server_default="ACTIVE")
    callback_config_ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)


class PaymentChannel(Base):
    __tablename__ = "payment_channels"
    __table_args__ = (
        UniqueConstraint("payment_method", "channel_code", "protocol", name="uq_channel"),
        CheckConstraint(
            _in("payment_method", ("VA", "QRIS", "EWALLET", "RETAIL")), name="payment_method"
        ),
        CheckConstraint(_in("protocol", ("SNAP", "LEGACY")), name="protocol"),
        CheckConstraint(_in("status", ("ACTIVE", "INACTIVE")), name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    payment_method: Mapped[str] = mapped_column(String(16))
    channel_code: Mapped[str] = mapped_column(String(32))
    protocol: Mapped[str] = mapped_column(String(8))
    display_name: Mapped[str] = mapped_column(String(80))
    provider_category: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)


class ClientMerchantRoute(Base):
    __tablename__ = "client_merchant_routes"
    __table_args__ = (
        UniqueConstraint(
            "client_id",
            "route_code",
            "payment_channel_id",
            "merchant_account_id",
            name="uq_route",
        ),
        CheckConstraint(_in("status", ("ACTIVE", "INACTIVE")), name="status"),
        Index(
            "ux_route_active_resolution",
            "client_id",
            "route_code",
            "payment_method",
            "channel_code",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "ux_route_active_default",
            "client_id",
            "payment_method",
            unique=True,
            postgresql_where=text("is_default AND status = 'ACTIVE'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_clients.id")
    )
    merchant_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_accounts.id")
    )
    payment_channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_channels.id")
    )
    payment_method: Mapped[str] = mapped_column(String(16))
    channel_code: Mapped[str] = mapped_column(String(32))
    route_code: Mapped[str] = mapped_column(String(64), server_default="DEFAULT")
    priority: Mapped[int] = mapped_column(Integer, server_default="100")
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(20), server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)


TERMINAL_STATUSES = ("FAILED", "EXPIRED", "CANCELLED")


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"
    __table_args__ = (
        UniqueConstraint(
            "merchant_account_id", "provider_partner_reference", name="uq_partner_reference"
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("currency = 'IDR'", name="currency"),
        CheckConstraint(
            _in("payment_method", ("VA", "QRIS", "EWALLET", "RETAIL")), name="payment_method"
        ),
        CheckConstraint(_in("protocol", ("SNAP", "LEGACY")), name="protocol"),
        CheckConstraint(
            _in(
                "status",
                ("CREATED", "PENDING", "SUCCEEDED", "FAILED", "EXPIRED", "CANCELLED", "UNKNOWN"),
            ),
            name="status",
        ),
        Index(
            "ux_payment_live_reference",
            "client_id",
            "merchant_reference",
            unique=True,
            postgresql_where=text("status NOT IN ('FAILED', 'EXPIRED', 'CANCELLED')"),
        ),
        Index(
            "ux_payment_provider_reference",
            "merchant_account_id",
            "protocol",
            "provider_reference",
            unique=True,
            postgresql_where=text("provider_reference IS NOT NULL"),
        ),
        Index(
            "ix_payment_reference_created",
            "client_id",
            "merchant_reference",
            text("created_at DESC"),
        ),
        Index(
            "ix_payment_open_status_updated",
            "status",
            "updated_at",
            postgresql_where=text("status IN ('CREATED', 'PENDING', 'UNKNOWN')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_clients.id")
    )
    merchant_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_accounts.id")
    )
    payment_channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_channels.id")
    )
    protocol: Mapped[str] = mapped_column(String(8))
    channel_code: Mapped[str] = mapped_column(String(32))
    merchant_reference: Mapped[str] = mapped_column(String(128))
    provider_partner_reference: Mapped[str] = mapped_column(String(32))
    provider_reference: Mapped[str | None] = mapped_column(String(128))
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), server_default="IDR")
    payment_method: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), server_default="CREATED")
    provider_status: Mapped[str | None] = mapped_column(String(64))
    instruction: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    paid_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint("payment_id", "operation", "attempt_no", name="uq_attempt"),
        CheckConstraint(_in("operation", ("CREATE", "INQUIRY", "CANCEL")), name="operation"),
        CheckConstraint(
            _in("outcome", ("SUCCEEDED", "FAILED", "UNKNOWN", "PENDING")), name="outcome"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_transactions.id")
    )
    attempt_no: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(32))
    protocol: Mapped[str] = mapped_column(String(8))
    external_id: Mapped[str | None] = mapped_column(String(36))
    outcome: Mapped[str] = mapped_column(String(24))
    http_status: Mapped[int | None] = mapped_column(Integer)
    provider_response_code: Mapped[str | None] = mapped_column(String(16))
    provider_reference: Mapped[str | None] = mapped_column(String(128))
    request_fingerprint: Mapped[str | None] = mapped_column(String(128))
    error_category: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint(_in("protocol", ("SNAP", "LEGACY")), name="protocol"),
        CheckConstraint(
            _in("processing_status", ("PROCESSED", "UNMATCHED", "REJECTED", "FAILED")),
            name="processing_status",
        ),
        Index(
            "ix_webhook_events_review",
            "processing_status",
            "received_at",
            postgresql_where=text("processing_status IN ('UNMATCHED', 'FAILED')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    merchant_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_accounts.id")
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_transactions.id")
    )
    protocol: Mapped[str] = mapped_column(String(8))
    source_path: Mapped[str] = mapped_column(String(128))
    provider_event_key: Mapped[str | None] = mapped_column(String(255))
    event_fingerprint: Mapped[str] = mapped_column(String(128), unique=True)
    event_type: Mapped[str] = mapped_column(String(64))
    provider_status_code: Mapped[str | None] = mapped_column(String(8))
    processing_status: Mapped[str] = mapped_column(String(24))
    signature_valid: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    verification_method: Mapped[str] = mapped_column(String(32))
    payload_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    processed_at: Mapped[datetime | None] = mapped_column(TS)


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    __table_args__ = (
        CheckConstraint(
            _in(
                "event_type",
                (
                    "payment.created",
                    "payment.pending",
                    "payment.succeeded",
                    "payment.failed",
                    "payment.expired",
                    "payment.cancelled",
                    "payment.unknown",
                ),
            ),
            name="event_type",
        ),
        Index("ix_payment_events_payment_occurred", "payment_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_transactions.id")
    )
    webhook_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_events.id")
    )
    event_type: Mapped[str] = mapped_column(String(32))
    status_from: Mapped[str | None] = mapped_column(String(24))
    status_to: Mapped[str] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("payment_event_id", "client_id", name="uq_delivery_event_client"),
        CheckConstraint(
            _in("delivery_status", ("PENDING", "RETRY_SCHEDULED", "DELIVERED", "DEAD_LETTER")),
            name="delivery_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    payment_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_events.id")
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_clients.id")
    )
    destination_url: Mapped[str] = mapped_column(Text)
    delivery_status: Mapped[str] = mapped_column(String(24), server_default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_error_category: Mapped[str | None] = mapped_column(String(64))
    next_retry_at: Mapped[datetime | None] = mapped_column(TS)
    delivered_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)


class GatewayJob(Base):
    __tablename__ = "gateway_jobs"
    __table_args__ = (
        CheckConstraint(
            _in(
                "job_type",
                (
                    "DELIVER_WEBHOOK",
                    "RETRY_CORRELATION",
                    "INQUIRE_PAYMENT",
                    "RECONCILE_PAYMENT",
                    "EXPIRE_PAYMENT",
                    "PURGE_RETENTION",
                ),
            ),
            name="job_type",
        ),
        CheckConstraint(
            _in("status", ("PENDING", "RUNNING", "RETRY", "DONE", "DEAD")), name="status"
        ),
        Index(
            "ix_gateway_jobs_claim",
            "status",
            "run_after",
            postgresql_where=text("status IN ('PENDING', 'RETRY', 'RUNNING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), server_default="PENDING")
    dedupe_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    args: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="8")
    run_after: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    locked_until: Mapped[datetime | None] = mapped_column(TS)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    last_error_category: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class ApiIdempotencyKey(Base):
    __tablename__ = "api_idempotency_keys"
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_idempotency_client_key"),
        CheckConstraint(
            _in("operation_status", ("IN_PROGRESS", "COMPLETED", "UNKNOWN", "FAILED")),
            name="operation_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_clients.id")
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(128))
    operation_status: Mapped[str] = mapped_column(String(24), server_default="IN_PROGRESS")
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_transactions.id")
    )
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    resolved_at: Mapped[datetime | None] = mapped_column(TS)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(_in("actor_type", ("CLIENT", "ADMIN", "SYSTEM")), name="actor_type"),
        Index("ix_audit_entity", "entity_type", "entity_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_transactions.id")
    )
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    metadata_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=NOW)
