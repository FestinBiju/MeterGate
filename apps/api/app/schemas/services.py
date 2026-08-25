"""Typed management contracts for merchant services."""

from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from app.domain.canonical_json import CanonicalJSONError, canonical_json_bytes
from app.domain.enums import PurchaseType, ServiceStatus, ServiceType
from app.domain.hashing import MAX_CANONICAL_INTEGER
from app.domain.json_schema import JSONSchemaConfigurationError, validate_json_schema_document
from app.schemas.common import (
    APIModel,
    CurrencyCode,
    Description,
    JSONObject,
    Name,
    ORMResponseModel,
    OutputContentType,
    Slug,
)
from app.schemas.common import reject_empty_or_null_patch as validate_patch_document

MinorUnitAmount = Annotated[StrictInt, Field(ge=0, le=MAX_CANONICAL_INTEGER)]
FulfillmentSeconds = Annotated[StrictInt, Field(ge=1, le=86_400)]


class ServiceCreate(APIModel):
    slug: Slug
    name: Name
    description: Description
    status: ServiceStatus = ServiceStatus.DRAFT
    service_type: ServiceType
    purchase_type: PurchaseType
    currency: CurrencyCode
    base_price: MinorUnitAmount
    input_schema: JSONObject
    output_schema: JSONObject
    output_content_type: OutputContentType
    maximum_fulfillment_seconds: FulfillmentSeconds
    refund_on_fulfillment_failure: StrictBool

    @field_validator("input_schema", "output_schema")
    @classmethod
    def validate_json_schema(cls, value: JSONObject) -> JSONObject:
        return _validate_service_json_schema(value)


class ServicePatch(APIModel):
    slug: Slug | None = None
    name: Name | None = None
    description: Description | None = None
    status: ServiceStatus | None = None
    service_type: ServiceType | None = None
    purchase_type: PurchaseType | None = None
    currency: CurrencyCode | None = None
    base_price: MinorUnitAmount | None = None
    input_schema: JSONObject | None = None
    output_schema: JSONObject | None = None
    output_content_type: OutputContentType | None = None
    maximum_fulfillment_seconds: FulfillmentSeconds | None = None
    refund_on_fulfillment_failure: StrictBool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_empty_or_null_patch(cls, data: Any) -> Any:
        return validate_patch_document(data)

    @field_validator("input_schema", "output_schema")
    @classmethod
    def validate_json_schema(cls, value: JSONObject | None) -> JSONObject | None:
        if value is None:
            return value
        return _validate_service_json_schema(value)


class ServiceResponse(ORMResponseModel):
    id: str
    merchant_id: str
    slug: str
    name: str
    description: str
    status: ServiceStatus
    service_type: ServiceType
    purchase_type: PurchaseType
    currency: str
    base_price: int
    input_schema: JSONObject
    output_schema: JSONObject
    output_content_type: str
    maximum_fulfillment_seconds: int
    refund_on_fulfillment_failure: bool
    created_at: datetime
    updated_at: datetime


def _validate_service_json_schema(value: JSONObject) -> JSONObject:
    try:
        validate_json_schema_document(value)
        canonical_json_bytes(value)
    except (CanonicalJSONError, JSONSchemaConfigurationError) as error:
        raise ValueError(
            "must be a canonicalizable, self-contained Draft 2020-12 JSON Schema"
        ) from error
    return value
