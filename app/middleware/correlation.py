"""Pure ASGI middleware: correlation ID, structured log context, access log."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import correlation_id_var, get_logger

_VALID = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
_QUIET_PATHS = {"/health", "/ready"}
log = get_logger("http")


class CorrelationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = ""
        for name, value in scope.get("headers", []):
            if name == b"x-correlation-id":
                incoming = value.decode("latin-1")
                break
        corr = incoming if _VALID.match(incoming) else f"corr_{uuid.uuid4().hex[:12]}"
        token = correlation_id_var.set(corr)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=corr)
        started = time.perf_counter()
        status_holder: dict[str, Any] = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-correlation-id", corr.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in _QUIET_PATHS:
                log.info(
                    "request",
                    method=scope.get("method"),
                    path=path,
                    status=status_holder["status"],
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            correlation_id_var.reset(token)
            structlog.contextvars.clear_contextvars()
