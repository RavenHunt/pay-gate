from __future__ import annotations


class RetryJob(Exception):  # noqa: N818 - control-flow signal, not an error condition
    """Reschedule with backoff (or an explicit delay in seconds)."""

    def __init__(self, category: str, delay: float | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.delay = delay


class DeadJob(Exception):  # noqa: N818
    """Permanent failure: mark DEAD without further retries."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category
