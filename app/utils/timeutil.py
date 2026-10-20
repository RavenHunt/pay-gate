"""Time helpers. Jakarta is a fixed UTC+7 offset (no DST)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

JAKARTA = timezone(timedelta(hours=7), name="Asia/Jakarta")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_jakarta(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(JAKARTA)


def iso_jakarta(value: datetime | None) -> str | None:
    """ISO-8601 with +07:00 offset, seconds precision."""
    if value is None:
        return None
    return to_jakarta(value).isoformat(timespec="seconds")


def snap_timestamp(value: datetime | None = None) -> str:
    return iso_jakarta(value or utcnow()) or ""


def legacy_datetime(value: datetime) -> str:
    """Faspay legacy `Y-m-d H:i:s` in Jakarta local time."""
    return to_jakarta(value).strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp with offset (naive values are rejected)."""
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no UTC offset")
    return parsed
