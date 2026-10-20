from __future__ import annotations

import random
from collections.abc import Callable


def compute_backoff(
    attempts: int,
    *,
    base: float,
    cap: float,
    jitter_fraction: float,
    rng: Callable[[], float] = random.random,
) -> float:
    """min(cap, base * 2^(attempts-1)) plus up to `jitter_fraction` random jitter."""
    exponent = max(attempts - 1, 0)
    delay = float(min(cap, base * (2**exponent)))
    return delay + delay * jitter_fraction * rng()
