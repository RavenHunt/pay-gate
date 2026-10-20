"""Error envelope (TRD section 7) and exception handlers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.observability import correlation_id_var, get_logger

log = get_logger("errors")


class GatewayError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}
        self.headers = headers or {}


class OAuthError(Exception):
    """RFC 6749 error response shape, used only by the token endpoint."""

    def __init__(
        self,
        error: str,
        description: str,
        status_code: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code
        self.headers = headers or {}


def error_body(
    code: str, message: str, details: list[dict[str, Any]] | None = None, **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": correlation_id_var.get() or None,
        }
    }
    if details:
        body["error"]["details"] = details
    body["error"].update(extra)
    return body


def _sanitize_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    # Never echo submitted values back: they may contain personal data.
    return [
        {"field": ".".join(str(p) for p in err.get("loc", ())), "message": err.get("msg", "")}
        for err in errors
    ]


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, **exc.extra),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(OAuthError)
    async def _oauth_error(_: Request, exc: OAuthError) -> JSONResponse:
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache", **exc.headers}
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description},
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        malformed = any(e.get("type") in ("json_invalid", "model_attributes_type") for e in errors)
        if malformed:
            return JSONResponse(
                error_body("MALFORMED_REQUEST", "The request body is not valid JSON."),
                status_code=400,
            )
        return JSONResponse(
            error_body(
                "VALIDATION_ERROR",
                "The request has invalid fields.",
                _sanitize_validation_errors(errors),
            ),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            error_body(code, str(exc.detail)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", error_type=type(exc).__name__, exc_info=exc)
        return JSONResponse(
            error_body("INTERNAL_ERROR", "An unexpected error occurred."), status_code=500
        )


# Common error constructors -------------------------------------------------


def unauthorized(message: str = "Missing or invalid access token.") -> GatewayError:
    return GatewayError(
        401, "UNAUTHORIZED", message, headers={"WWW-Authenticate": 'Bearer realm="gateway"'}
    )


def forbidden(code: str, message: str) -> GatewayError:
    return GatewayError(403, code, message)


def not_found(message: str = "Payment not found.") -> GatewayError:
    return GatewayError(404, "PAYMENT_NOT_FOUND", message)
