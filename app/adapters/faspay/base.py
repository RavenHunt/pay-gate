"""Internal adapter interface shared by the SNAP and Legacy drivers (TRD 9.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from app.adapters.faspay.status_map import StatusMapping
from app.domain import PaymentStatus
from app.domain import Protocol as ProviderProtocol


class CreateOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"  # confirmed: the provider did not create a payment
    UNKNOWN = "UNKNOWN"  # ambiguous: it may have been created


class InquiryOutcome(StrEnum):
    OK = "OK"
    NOT_FOUND = "NOT_FOUND"
    NOT_INQUIRABLE = "NOT_INQUIRABLE"
    ERROR = "ERROR"


class InquiryPolicy(StrEnum):
    NONE = "NONE"  # the signature is the evidence
    ON_SUCCESS = "ON_SUCCESS"  # confirm a success with an inquiry first
    ALWAYS = "ALWAYS"  # the inquiry result is the status (Legacy, unsigned SNAP)


@dataclass
class CustomerInfo:
    name: str | None = None
    email: str | None = None
    phone: str | None = None


@dataclass
class CreateContext:
    payment_id: str
    partner_reference: str  # becomes partnerReferenceNo / trxId / bill_no
    amount: int  # integer rupiah
    payment_method: str
    channel_code: str
    channel: dict[str, Any]
    merchant: dict[str, Any]
    customer: CustomerInfo
    expires_at: datetime
    created_at: datetime
    external_id: str  # SNAP X-EXTERNAL-ID
    description: str | None = None


@dataclass
class CreateResult:
    outcome: CreateOutcome
    provider_reference: str | None = None
    instruction: dict[str, Any] = field(default_factory=dict)
    provider_status: str | None = None
    response_code: str | None = None
    response_desc: str | None = None
    http_status: int | None = None
    error_category: str | None = None
    request_fingerprint: str | None = None


@dataclass
class InquiryTarget:
    """Everything an inquiry needs; built from a stored payment or a callback."""

    protocol: str
    payment_method: str
    partner_reference: str
    provider_reference: str | None
    channel_code: str | None
    merchant: dict[str, Any]
    amount: int | None = None
    snap_extras: dict[str, Any] = field(default_factory=dict)  # partnerServiceId, customerNo
    expected_status_hint: str | None = None


@dataclass
class InquiryResult:
    outcome: InquiryOutcome
    status: PaymentStatus | None = None
    provider_status: str | None = None
    provider_reference: str | None = None
    amount: int | None = None
    paid_at: datetime | None = None
    response_code: str | None = None
    http_status: int | None = None
    error_category: str | None = None
    mapping: StatusMapping | None = None


@dataclass
class VerifiedEvent:
    protocol: str
    source_path: str
    merchant: dict[str, Any]
    shape: str  # VA | REFERENCE | LEGACY
    partner_reference: str
    provider_reference: str | None
    amount: int | None
    currency: str | None
    provider_status: str
    status: PaymentStatus | None
    mapping: StatusMapping
    paid_at: datetime | None
    channel_code: str | None
    fingerprint: str
    payload_redacted: dict[str, Any]
    signature_valid: bool
    verification_method: str
    inquiry_policy: InquiryPolicy = InquiryPolicy.NONE
    inquiry_done: bool = False
    extras: dict[str, Any] = field(default_factory=dict)
    event_type: str = "payment.notification"


@dataclass
class CancelResult:
    supported: bool = False
    outcome: str = "NOT_SUPPORTED"


class FaspayDriver(Protocol):
    protocol: ProviderProtocol

    async def create_payment(self, ctx: CreateContext) -> CreateResult: ...

    async def inquire_payment(self, target: InquiryTarget) -> InquiryResult: ...

    async def cancel_payment(self, target: InquiryTarget) -> CancelResult: ...

    def normalize_status(self, raw: object) -> StatusMapping: ...
