"""Create and read payments (TRD 6, 10, 14).

Create runs as: validate -> idempotency/route -> short transaction (claim, insert
payment + attempt + created event) -> provider call OUTSIDE any transaction ->
short transaction (persist outcome).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.adapters.faspay.base import (
    CreateContext,
    CreateOutcome,
    CreateResult,
    CustomerInfo,
)
from app.adapters.faspay.registry import get_driver
from app.config import ConfigError, get_settings
from app.database import transaction
from app.dependencies import AuthContext
from app.domain import JobType, PaymentStatus
from app.exceptions import GatewayError, not_found
from app.observability import alert, correlation_id_var, emit_metric, get_logger
from app.repositories import attempts as attempts_repo
from app.repositories import events as events_repo
from app.repositories import idempotency as idempotency_repo
from app.repositories import jobs as jobs_repo
from app.repositories import merchants as merchants_repo
from app.repositories import payments as payments_repo
from app.schemas.payments import CreatePaymentRequest, payment_view
from app.services import routing
from app.services.transitions import apply_transition
from app.utils.hashing import canonical_json, sha256_hex
from app.utils.ids import new_partner_reference, uuid7
from app.utils.timeutil import utcnow

log = get_logger("payments")


@dataclass
class CreateResponse:
    http_status: int
    body: dict[str, Any]


def request_fingerprint(req: CreatePaymentRequest) -> str:
    """Canonical business request; the idempotency key itself is not part of it."""
    customer = req.customer.model_dump() if req.customer else None
    expires = req.expires_at.astimezone(utcnow().tzinfo).isoformat() if req.expires_at else None
    return sha256_hex(
        canonical_json(
            {
                "merchant_reference": req.merchant_reference,
                "amount": req.amount,
                "currency": req.currency,
                "payment_method": req.payment_method.value,
                "channel_code": req.channel_code,
                "route_code": req.route_code,
                "description": req.description,
                "customer": customer,
                "expires_at": expires,
                "metadata": req.metadata,
            }
        )
    )


def compute_expiry(req: CreatePaymentRequest, protocol: str, method: str) -> Any:
    settings = get_settings()
    now = utcnow().replace(microsecond=0)
    max_minutes = settings.EXPIRES_MAX_MINUTES_BY_METHOD.get(method, 1440)
    if req.expires_at is None:
        default = settings.EXPIRES_DEFAULT_MINUTES_BY_PROTOCOL.get(protocol, 300)
        return now + timedelta(minutes=min(default, max_minutes))
    expires = req.expires_at.astimezone(now.tzinfo).replace(microsecond=0)
    if expires < now + timedelta(minutes=settings.EXPIRES_MIN_MINUTES):
        raise GatewayError(
            422,
            "EXPIRES_AT_TOO_SOON",
            f"expires_at must be at least {settings.EXPIRES_MIN_MINUTES} minutes ahead.",
        )
    if expires > now + timedelta(minutes=max_minutes):
        raise GatewayError(
            422,
            "EXPIRES_AT_TOO_FAR",
            f"expires_at may be at most {max_minutes} minutes ahead for {method}.",
        )
    return expires


# ── Create ────────────────────────────────────────────────────────────


async def create_payment(
    auth: AuthContext, req: CreatePaymentRequest, idempotency_key: str
) -> CreateResponse:
    fingerprint = request_fingerprint(req)

    async with transaction() as conn:
        existing = await idempotency_repo.get(conn, auth.client_id, idempotency_key)
    if existing:
        return await _replay(auth, existing, fingerprint)

    route = await routing.resolve_route(
        auth.client_id, req.payment_method.value, req.channel_code, req.route_code
    )
    expires_at = compute_expiry(req, route["protocol"], route["payment_method"])
    payment_id = uuid7()
    external_id = str(uuid.uuid4())

    for attempt in range(3):
        partner_reference = new_partner_reference()
        try:
            async with transaction() as conn:
                claimed = await idempotency_repo.claim(
                    conn, client_id=auth.client_id, key=idempotency_key, fingerprint=fingerprint
                )
                if not claimed:
                    existing = await idempotency_repo.get(conn, auth.client_id, idempotency_key)
                else:
                    await payments_repo.insert_payment(
                        conn,
                        payment_id=payment_id,
                        client_id=auth.client_id,
                        route=route,
                        merchant_reference=req.merchant_reference,
                        partner_reference=partner_reference,
                        amount=req.amount,
                        metadata=req.metadata,
                        expires_at=expires_at,
                    )
                    await idempotency_repo.attach_payment(
                        conn, auth.client_id, idempotency_key, payment_id
                    )
                    await events_repo.insert_payment_event(
                        conn,
                        payment_id=payment_id,
                        webhook_event_id=None,
                        event_type="payment.created",
                        status_from=None,
                        status_to=PaymentStatus.CREATED.value,
                        source="CREATE",
                    )
                    attempt_id = await attempts_repo.start_attempt(
                        conn,
                        payment_id=payment_id,
                        operation="CREATE",
                        protocol=route["protocol"],
                        external_id=external_id if route["protocol"] == "SNAP" else None,
                    )
            if not claimed:
                assert existing is not None
                return await _replay(auth, existing, fingerprint)
            break
        except IntegrityError as exc:
            constraint = _constraint_name(exc)
            if constraint == "uq_partner_reference" and attempt < 2:
                continue  # astronomically rare: regenerate the compact reference
            if constraint == "ux_payment_live_reference":
                raise GatewayError(
                    409,
                    "MERCHANT_REFERENCE_CONFLICT",
                    "A payment for this merchant_reference is already in progress or succeeded.",
                ) from exc
            raise
    else:  # pragma: no cover
        raise GatewayError(500, "INTERNAL_ERROR", "Could not allocate a payment reference.")

    return await _submit_to_provider(
        auth,
        route=route,
        payment_id=payment_id,
        attempt_id=attempt_id,
        partner_reference=partner_reference,
        external_id=external_id,
        req=req,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
    )


def _constraint_name(exc: IntegrityError) -> str:
    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return str(orig)


async def _submit_to_provider(
    auth: AuthContext,
    *,
    route: dict[str, Any],
    payment_id: uuid.UUID,
    attempt_id: uuid.UUID,
    partner_reference: str,
    external_id: str,
    req: CreatePaymentRequest,
    expires_at: Any,
    idempotency_key: str,
) -> CreateResponse:
    merchant = {
        "id": route["merchant_account_id"],
        "merchant_code": route["merchant_code"],
        "credential_ref": route["credential_ref"],
        "partner_id": route["partner_id"],
        "provider_merchant_id": route["provider_merchant_id"],
        "merchant_name": route["merchant_name"],
    }
    ctx = CreateContext(
        payment_id=str(payment_id),
        partner_reference=partner_reference,
        amount=req.amount,
        payment_method=route["payment_method"],
        channel_code=route["channel_code"],
        channel={
            "display_name": route["display_name"],
            "provider_category": route["provider_category"],
        },
        merchant=merchant,
        customer=CustomerInfo(
            name=req.customer.name if req.customer else None,
            email=req.customer.email if req.customer else None,
            phone=req.customer.phone if req.customer else None,
        ),
        expires_at=expires_at,
        created_at=utcnow(),
        external_id=external_id,
        description=req.description,
    )

    # The provider call happens with no database transaction open.
    try:
        result = await get_driver(route["protocol"]).create_payment(ctx)
    except ConfigError as exc:
        alert("merchant_credentials_unavailable", merchant_code=route["merchant_code"])
        log.error("create_config_error", error=str(exc))
        result = CreateResult(CreateOutcome.FAILED, error_category="config_error")
    except Exception as exc:  # noqa: BLE001 - we cannot know whether it was sent
        log.error("create_unexpected_error", error_type=type(exc).__name__, exc_info=exc)
        result = CreateResult(CreateOutcome.UNKNOWN, error_category="unexpected_error")

    emit_metric(
        "payment_create",
        protocol=route["protocol"],
        merchant_code=route["merchant_code"],
        outcome=result.outcome.value,
        error_category=result.error_category,
    )

    try:
        await _persist_outcome(route, payment_id, attempt_id, auth, idempotency_key, result)
    except IntegrityError as exc:
        # e.g. the provider reference already belongs to another payment: park as UNKNOWN.
        alert("create_persist_conflict", payment_id=str(payment_id), error=_constraint_name(exc))
        result = CreateResult(
            CreateOutcome.UNKNOWN,
            http_status=result.http_status,
            response_code=result.response_code,
            error_category="persist_conflict",
        )
        await _persist_outcome(route, payment_id, attempt_id, auth, idempotency_key, result)

    async with transaction() as conn:
        row = await payments_repo.get_by_id(conn, payment_id)
    assert row is not None
    return _create_response(row, first_time=True)


def _can_inquire(route: dict[str, Any]) -> bool:
    # A SNAP VA lost create response cannot be inquired: the VA number is unknown.
    return not (route["protocol"] == "SNAP" and route["payment_method"] == "VA")


async def _persist_outcome(
    route: dict[str, Any],
    payment_id: uuid.UUID,
    attempt_id: uuid.UUID,
    auth: AuthContext,
    idempotency_key: str,
    result: CreateResult,
) -> None:
    settings = get_settings()
    async with transaction() as conn:
        payment = await payments_repo.get_by_id(conn, payment_id, for_update=True)
        assert payment is not None
        await attempts_repo.finish_attempt(
            conn,
            attempt_id,
            outcome=result.outcome.value,
            http_status=result.http_status,
            provider_response_code=result.response_code,
            provider_reference=result.provider_reference,
            request_fingerprint=result.request_fingerprint,
            error_category=result.error_category,
        )
        if result.outcome == CreateOutcome.SUCCEEDED:
            await payments_repo.update_after_create(
                conn,
                payment_id,
                provider_reference=result.provider_reference,
                instruction=result.instruction,
                provider_status=result.provider_status,
            )
            await apply_transition(
                conn, payment, PaymentStatus.PENDING, source="CREATE",
                provider_status=result.provider_status,
            )  # fmt: skip
            await idempotency_repo.resolve(
                conn, auth.client_id, idempotency_key, operation_status="COMPLETED", snapshot=None
            )
            # A callback may have arrived before the references were saved.
            for pending in await events_repo.unmatched_for_partner_reference(
                conn, payment["merchant_account_id"], payment["provider_partner_reference"]
            ):
                await jobs_repo.enqueue(
                    conn,
                    JobType.RETRY_CORRELATION,
                    ref_id=pending["id"],
                    dedupe_key=f"recorr:{pending['id']}:create",
                    max_attempts=settings.CORRELATION_MAX_ATTEMPTS,
                )
            return

        if result.outcome == CreateOutcome.FAILED:
            await apply_transition(conn, payment, PaymentStatus.FAILED, source="CREATE")
            await idempotency_repo.resolve(
                conn, auth.client_id, idempotency_key, operation_status="FAILED", snapshot=None
            )
            return

        # UNKNOWN: never resubmit, never switch protocol; resolve by inquiry or callback.
        await apply_transition(conn, payment, PaymentStatus.UNKNOWN, source="CREATE")
        await idempotency_repo.resolve(
            conn, auth.client_id, idempotency_key, operation_status="UNKNOWN", snapshot=None
        )
        if _can_inquire(route):
            await jobs_repo.enqueue(
                conn,
                JobType.INQUIRE_PAYMENT,
                ref_id=payment_id,
                dedupe_key=f"inquire:{payment_id}:create",
                delay_seconds=settings.UNKNOWN_INQUIRY_DELAY_SECONDS,
                max_attempts=settings.INQUIRY_MAX_ATTEMPTS,
            )
        else:
            alert("unknown_create_not_inquirable", payment_id=str(payment_id))


def _create_response(row: dict[str, Any], *, first_time: bool) -> CreateResponse:
    view = payment_view(row)
    state = PaymentStatus(row["status"])
    if state == PaymentStatus.FAILED:
        raise GatewayError(
            502,
            "PROVIDER_REJECTED",
            "The payment provider rejected or could not accept the payment.",
            extra={"payment_id": str(row["id"]), "status": "FAILED"},
        )
    if state in (PaymentStatus.UNKNOWN, PaymentStatus.CREATED):
        view["correlation_id"] = correlation_id_var.get() or None
        return CreateResponse(202, view)
    if state == PaymentStatus.PENDING and first_time:
        return CreateResponse(201, view)
    return CreateResponse(200, view)


async def _replay(auth: AuthContext, existing: dict[str, Any], fingerprint: str) -> CreateResponse:
    if existing["request_fingerprint"] != fingerprint:
        raise GatewayError(
            409,
            "IDEMPOTENCY_KEY_REUSED",
            "The idempotency key was previously used with a different request.",
        )
    if existing["payment_id"] is None:
        raise GatewayError(
            409, "REQUEST_IN_PROGRESS", "A request with this idempotency key is in progress."
        )
    async with transaction() as conn:
        row = await payments_repo.get_for_client(conn, auth.client_id, existing["payment_id"])
    if row is None:  # pragma: no cover - FK guarantees it
        raise not_found()
    return _create_response(row, first_time=False)


# ── Read ──────────────────────────────────────────────────────────────


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise not_found() from exc


async def get_payment(auth: AuthContext, payment_id: str) -> dict[str, Any]:
    pid = _parse_uuid(payment_id)
    async with transaction() as conn:
        row = await payments_repo.get_for_client(conn, auth.client_id, pid)
    if row is None:
        raise not_found()
    return payment_view(row)


async def get_payment_by_reference(auth: AuthContext, merchant_reference: str) -> dict[str, Any]:
    async with transaction() as conn:
        row = await payments_repo.get_latest_by_reference(conn, auth.client_id, merchant_reference)
    if row is None:
        raise not_found()  # same answer whether or not another client owns it
    return payment_view(row)


async def load_merchant(merchant_account_id: uuid.UUID) -> dict[str, Any]:
    async with transaction() as conn:
        merchant = await merchants_repo.get_merchant(conn, merchant_account_id)
    if merchant is None:  # pragma: no cover - FK guarantees it
        raise GatewayError(500, "INTERNAL_ERROR", "Merchant account missing.")
    return merchant
