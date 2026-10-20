"""Per-protocol status tables (TRD 9.5). Raw status is always stored separately.

Faspay's documentation is ambiguous for some codes, so each table can be
overridden by configuration (`SNAP_STATUS_MAP_OVERRIDES`,
`LEGACY_STATUS_MAP_OVERRIDES`). An override value is a normalized status name,
or `RECORD` to store the raw value without applying it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.domain import PaymentStatus

S = PaymentStatus


@dataclass(frozen=True)
class StatusMapping:
    status: PaymentStatus | None  # None: recorded only, status unchanged
    alert: str | None = None
    enqueue_inquiry: bool = False


SNAP_TABLE: dict[str, StatusMapping] = {
    "00": StatusMapping(S.SUCCEEDED),
    "01": StatusMapping(S.PENDING),  # Initiated (callback) / Paying (VA inquiry)
    "02": StatusMapping(S.PENDING),  # Paying (callback list)
    "03": StatusMapping(S.PENDING),
    "04": StatusMapping(None, alert="refund_review"),  # refund is out of scope
    "05": StatusMapping(S.CANCELLED),
    "06": StatusMapping(S.FAILED),
    "07": StatusMapping(S.EXPIRED),  # also listed as "Not found": confirm with Faspay
}

LEGACY_TABLE: dict[str, StatusMapping] = {
    "2": StatusMapping(S.SUCCEEDED),  # the only value confirmed by the existing code
    "0": StatusMapping(S.PENDING),
    "1": StatusMapping(S.PENDING),
    "3": StatusMapping(S.FAILED),
    "7": StatusMapping(S.EXPIRED),
    "8": StatusMapping(S.CANCELLED),
    "4": StatusMapping(None, alert="reversal_review"),
    "5": StatusMapping(None, alert="legacy_status_unclear", enqueue_inquiry=True),
    "9": StatusMapping(None, alert="legacy_status_unclear", enqueue_inquiry=True),
}


def _apply_overrides(
    table: dict[str, StatusMapping], overrides: dict[str, str]
) -> dict[str, StatusMapping]:
    merged = dict(table)
    for raw, target in overrides.items():
        if target.upper() == "RECORD":
            merged[str(raw)] = StatusMapping(None, alert="status_recorded_only")
        else:
            merged[str(raw)] = StatusMapping(PaymentStatus(target.upper()))
    return merged


def _lookup(table: dict[str, StatusMapping], raw: object) -> StatusMapping:
    key = "" if raw is None else str(raw).strip()
    return table.get(key, StatusMapping(None, alert="unmapped_provider_status"))


def map_snap_status(raw: object) -> StatusMapping:
    table = _apply_overrides(SNAP_TABLE, get_settings().SNAP_STATUS_MAP_OVERRIDES)
    return _lookup(table, raw)


def map_legacy_status(raw: object) -> StatusMapping:
    table = _apply_overrides(LEGACY_TABLE, get_settings().LEGACY_STATUS_MAP_OVERRIDES)
    return _lookup(table, raw)
