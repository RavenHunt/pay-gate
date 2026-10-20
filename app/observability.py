"""Structured logging (structlog) with redaction, and metric events.

There is no metrics backend in the MVP: metrics are structured log events
(`event="metric"`) labelled with `protocol` and `merchant_code`, ready for a
log-drain based dashboard.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")

_SENSITIVE_PARTS = (
    "authorization",
    "signature",
    "secret",
    "password",
    "private_key",
    "token",
    "api_key",
    "credential",
)


def _redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        lowered = key.lower()
        if lowered != "event" and any(part in lowered for part in _SENSITIVE_PARTS):
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging(level: str = "info", debug: bool = False) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer() if debug else structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "gateway") -> Any:
    return structlog.get_logger(name)


def emit_metric(name: str, value: float = 1, **labels: Any) -> None:
    """Counter/gauge as a structured event. Labels should carry protocol, merchant_code."""
    get_logger("metric").info("metric", metric=name, value=value, **labels)


def alert(code: str, **fields: Any) -> None:
    """An operator-facing alert condition (log-drain alert rules key on `alert`)."""
    get_logger("alert").warning("alert", alert=code, **fields)
