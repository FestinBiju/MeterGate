"""Public contracts for paid entitlements, 402 challenges, and fulfillment."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictInt, StringConstraints

from app.domain.enums import PurchaseType
from app.schemas.common import APIModel, CurrencyCode, OutputContentType, Slug
from app.schemas.payments import PaymentTransactionId

EntitlementId = Annotated[
    str,
    StringConstraints(pattern=r"^ent_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
FulfillmentExecutionId = Annotated[
    str,
    StringConstraints(pattern=r"^ful_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
HashValue = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
BearerToken = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class ResourceParty(APIModel):
    id: str
    slug: Slug
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class EntitlementResponse(APIModel):
    entitlement_id: EntitlementId
    transaction_id: PaymentTransactionId
    merchant: ResourceParty
    service: ResourceParty
    input: JsonValue
    input_hash: HashValue
    amount: Annotated[StrictInt, Field(ge=1, le=(1 << 53) - 1)]
    currency: CurrencyCode
    purchase_type: PurchaseType
    maximum_executions: Literal[1]
    issued_at: datetime
    expires_at: datetime
    state: Literal["active", "expired"]
    entitlement_version: Literal["1"]
    entitlement_hash: HashValue


class EntitlementTimelineEvent(APIModel):
    event_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    reason_code: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    occurred_at: datetime


class EntitlementLookupResponse(APIModel):
    transaction_id: PaymentTransactionId
    state: Literal["pending", "ready", "blocked"]
    reason_code: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    entitlement: EntitlementResponse | None
    timeline: list[EntitlementTimelineEvent]


class CapabilityResponse(APIModel):
    entitlement_id: EntitlementId
    token: BearerToken
    expires_at: datetime
    maximum_executions: Literal[1]


class QuoteRequestDescription(APIModel):
    service_id: str
    input: JsonValue


class QuoteLink(APIModel):
    endpoint: Literal["/api/v1/quotes"] = "/api/v1/quotes"
    request: QuoteRequestDescription


class ResourceAccessDescription(APIModel):
    scheme: Literal["Bearer"] = "Bearer"
    maximum_executions: Literal[1] = 1


class PaymentRequiredResponse(APIModel):
    type: Literal["metergate_payment_required"] = "metergate_payment_required"
    protocol: Literal["metergate/1"] = "metergate/1"
    merchant: ResourceParty
    service: ResourceParty
    quote: QuoteLink
    payment_provider: Literal["razorpay"] = "razorpay"
    access: ResourceAccessDescription = ResourceAccessDescription()


class FulfillmentPendingResponse(APIModel):
    execution_id: FulfillmentExecutionId
    entitlement_id: EntitlementId
    state: Literal["executing"] = "executing"
    reason_code: Literal[
        "FULFILLMENT_ALREADY_CLAIMED",
        "FULFILLMENT_PROVIDER_IN_PROGRESS",
    ]


class FulfillmentResultResponse(APIModel):
    execution_id: FulfillmentExecutionId
    entitlement_id: EntitlementId
    result_content_type: OutputContentType
    result: JsonValue
    result_hash: HashValue
    result_size_bytes: Annotated[StrictInt, Field(ge=0, le=1_048_576)]
    replayed_result: bool
    completed_at: datetime
