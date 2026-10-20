"""Legacy Faspay signature: sha1(md5(user_id + password + bill_no))."""

from __future__ import annotations

import hashlib


def legacy_signature(user_id: str, password: str, bill_no: str) -> str:
    inner = hashlib.md5((user_id + password + bill_no).encode()).hexdigest()  # noqa: S324
    return hashlib.sha1(inner.encode()).hexdigest()  # noqa: S324


def legacy_notification_signature(
    user_id: str, password: str, bill_no: str, payment_status_code: str
) -> str:
    """Notification formula from Faspay's legacy conventions; unconfirmed, off by default."""
    inner = hashlib.md5((user_id + password + bill_no + payment_status_code).encode()).hexdigest()  # noqa: S324
    return hashlib.sha1(inner.encode()).hexdigest()  # noqa: S324
