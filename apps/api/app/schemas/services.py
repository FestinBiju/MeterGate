"""Typed management contracts for merchant services."""

from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StrictBool, StrictInt, model_validator

from app.domain.enums import PurchaseType, ServiceStatus, ServiceType
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

MinorUnitAmount = Annotated[StrictInt, Field(ge=0)]
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
