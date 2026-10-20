"""Tiny in-memory TTL cache. An optimization only, never a source of truth."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

K = TypeVar("K")
V = TypeVar("V")


class TTLCache[K, V]:
    def __init__(
        self,
        ttl_seconds: float,
        maxsize: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._maxsize = maxsize
        self._clock = clock
        self._data: dict[K, tuple[float, V]] = {}

    def get(self, key: K) -> V | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= self._clock():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: K, value: V) -> None:
        if len(self._data) >= self._maxsize:
            now = self._clock()
            for k in [k for k, (exp, _) in self._data.items() if exp <= now]:
                self._data.pop(k, None)
            if len(self._data) >= self._maxsize:
                self._data.pop(next(iter(self._data)), None)
        self._data[key] = (self._clock() + self._ttl, value)

    def clear(self) -> None:
        self._data.clear()

    def set_ttl(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
