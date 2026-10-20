"""SNAP notification acknowledgements (TRD 9.2, "Acknowledgement")."""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse

from app.utils.timeutil import snap_timestamp

SERVICE_VA = "25"
SERVICE_QRIS = "52"
SERVICE_EWALLET = "56"

ROUTE_SERVICE = {
    "/v1.0/transfer-va/payment": SERVICE_VA,
    "/v1.0/qr/qr-mpm-notify": SERVICE_QRIS,
    "/v1.0/debit/notify": SERVICE_EWALLET,
}
METHOD_SERVICE = {"VA": SERVICE_VA, "QRIS": SERVICE_QRIS, "EWALLET": SERVICE_EWALLET}

SUCCESS_MESSAGE = {SERVICE_VA: "Successful"}
DEFAULT_SUCCESS_MESSAGE = "Request has been processed successfully"


def snap_code(http_status: int, service: str, case: str) -> str:
    """7 characters: HTTP status (3) + service code (2) + case code (2)."""
    return f"{http_status}{service}{case}"


def _respond(http_status: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=http_status, headers={"X-TIMESTAMP": snap_timestamp()})


def success(service: str, echo: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {
        "responseCode": snap_code(200, service, "00"),
        "responseMessage": SUCCESS_MESSAGE.get(service, DEFAULT_SUCCESS_MESSAGE),
    }
    if service == SERVICE_VA and echo:
        body["virtualAccountData"] = {k: v for k, v in echo.items() if v is not None}
    return _respond(200, body)


def error(http_status: int, service: str, case: str, message: str) -> JSONResponse:
    return _respond(
        http_status,
        {"responseCode": snap_code(http_status, service, case), "responseMessage": message},
    )


def invalid_signature(service: str) -> JSONResponse:
    return error(401, service, "00", "Unauthorized. Signature")


def bad_format(service: str, case: str, message: str) -> JSONResponse:
    return error(400, service, case, message)


def not_found(service: str) -> JSONResponse:
    case = "12" if service == SERVICE_VA else "01"
    message = "Invalid Bill/Virtual Account" if service == SERVICE_VA else "Transaction Not Found"
    return error(404, service, case, message)


def invalid_amount(service: str) -> JSONResponse:
    return error(404, service, "13", "Invalid Amount")


def expired(service: str) -> JSONResponse:
    return error(404, service, "19", "Expired")


def duplicate(service: str) -> JSONResponse:
    return error(409, service, "01", "Duplicate partnerReferenceNo")


def gateway_error(service: str) -> JSONResponse:
    return error(500, service, "01", "Internal Server Error")
