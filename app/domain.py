"""Shared domain constants."""

from __future__ import annotations

from enum import StrEnum


class PaymentStatus(StrEnum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class PaymentMethod(StrEnum):
    VA = "VA"
    QRIS = "QRIS"
    EWALLET = "EWALLET"
    RETAIL = "RETAIL"


class Protocol(StrEnum):
    SNAP = "SNAP"
    LEGACY = "LEGACY"


class JobType(StrEnum):
    DELIVER_WEBHOOK = "DELIVER_WEBHOOK"
    RETRY_CORRELATION = "RETRY_CORRELATION"
    INQUIRE_PAYMENT = "INQUIRE_PAYMENT"
    RECONCILE_PAYMENT = "RECONCILE_PAYMENT"
    EXPIRE_PAYMENT = "EXPIRE_PAYMENT"
    PURGE_RETENTION = "PURGE_RETENTION"


FINAL_STATUSES = frozenset(
    {
        PaymentStatus.SUCCEEDED,
        PaymentStatus.FAILED,
        PaymentStatus.EXPIRED,
        PaymentStatus.CANCELLED,
    }
)
# A new payment may reuse the consumer reference only after one of these.
RELEASING_STATUSES = frozenset(
    {PaymentStatus.FAILED, PaymentStatus.EXPIRED, PaymentStatus.CANCELLED}
)

EVENT_TYPES = {
    PaymentStatus.CREATED: "payment.created",
    PaymentStatus.PENDING: "payment.pending",
    PaymentStatus.SUCCEEDED: "payment.succeeded",
    PaymentStatus.FAILED: "payment.failed",
    PaymentStatus.EXPIRED: "payment.expired",
    PaymentStatus.CANCELLED: "payment.cancelled",
    PaymentStatus.UNKNOWN: "payment.unknown",
}

# Recorded but not delivered by default (the consumer has them from the create response).
DELIVERED_EVENT_TYPES = frozenset(
    {
        "payment.succeeded",
        "payment.failed",
        "payment.expired",
        "payment.cancelled",
        "payment.unknown",
    }
)

SCOPE_PAYMENTS_WRITE = "payments:write"
SCOPE_PAYMENTS_READ = "payments:read"
ALL_SCOPES = (SCOPE_PAYMENTS_WRITE, SCOPE_PAYMENTS_READ)
