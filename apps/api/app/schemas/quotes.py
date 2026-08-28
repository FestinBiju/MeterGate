"""Strict public contracts for immutable server-issued quotes."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictBool, StrictInt, StringConstraints

from app.domain.enums import PurchaseType, ServiceType
from app.schemas.common import APIModel, CurrencyCode, JSONObject, OutputContentType

QuoteAmount = Annotated[StrictInt, Field(ge=0, le=(1 << 53) - 1)]
QuoteFulfillmentSeconds = Annotated[StrictInt, Field(ge=1, le=86_400)]
SHA256Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class QuoteCreate(APIModel):
    service_id: str
    input: JsonValue


class QuoteMerchant(APIModel):
    id: str
    slug: str
    name: str


class QuoteService(APIModel):
    id: str
    slug: str
    name: str
    service_type: ServiceType
    input_schema: JSONObject
    output_schema: JSONObject
    output_content_type: OutputContentType


class QuotePricing(APIModel):
    amount: QuoteAmount
    currency: CurrencyCode
    purchase_type: PurchaseType


class QuoteFulfillment(APIModel):
    maximum_seconds: QuoteFulfillmentSeconds
    refund_on_failure: StrictBool


class QuoteServiceSnapshot(APIModel):
    """Versioned, self-contained commercial service state at issuance."""

    version: Literal["1"] = "1"
    json_schema_dialect: Literal["https://json-schema.org/draft/2020-12/schema"] = (
        "https://json-schema.org/draft/2020-12/schema"
    )
    merchant: QuoteMerchant
    service: QuoteService
    pricing: QuotePricing
    fulfillment: QuoteFulfillment


class QuoteResponse(APIModel):
    id: str
    merchant: QuoteMerchant
    service: QuoteService
    input: JsonValue
    input_hash: SHA256Digest
    pricing: QuotePricing
    fulfillment: QuoteFulfillment
    issued_at: datetime
    expires_at: datetime
    state: Literal["active", "expired"]
    quote_hash: SHA256Digest
