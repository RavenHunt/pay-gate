"""Normative payment state machine (TRD 11.3)."""

from __future__ import annotations

from app.domain import FINAL_STATUSES, PaymentStatus

S = PaymentStatus

ALLOWED: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    S.CREATED: frozenset({S.PENDING, S.FAILED, S.UNKNOWN}),
    S.PENDING: frozenset({S.SUCCEEDED, S.FAILED, S.EXPIRED, S.CANCELLED, S.UNKNOWN}),
    S.UNKNOWN: frozenset({S.PENDING, S.SUCCEEDED, S.FAILED, S.EXPIRED, S.CANCELLED}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.EXPIRED: frozenset(),
    S.CANCELLED: frozenset(),
}


def can_transition(current: PaymentStatus | str, target: PaymentStatus | str) -> bool:
    return PaymentStatus(target) in ALLOWED[PaymentStatus(current)]


def is_final(status: PaymentStatus | str) -> bool:
    return PaymentStatus(status) in FINAL_STATUSES


def is_contradiction(current: PaymentStatus | str, target: PaymentStatus | str) -> bool:
    """A final payment receiving a different status needs alert and reconciliation."""
    return is_final(current) and PaymentStatus(current) != PaymentStatus(target)
