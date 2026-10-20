"""Driver lookup. Drivers are imported lazily to keep cold starts small."""

from __future__ import annotations

from app.adapters.faspay.base import FaspayDriver
from app.domain import Protocol

_drivers: dict[str, FaspayDriver] = {}


def get_driver(protocol: str) -> FaspayDriver:
    key = Protocol(protocol).value
    driver = _drivers.get(key)
    if driver is None:
        if key == Protocol.SNAP:
            from app.adapters.faspay.snap.client import SnapDriver

            driver = SnapDriver()
        else:
            from app.adapters.faspay.legacy.client import LegacyDriver

            driver = LegacyDriver()
        _drivers[key] = driver
    return driver
