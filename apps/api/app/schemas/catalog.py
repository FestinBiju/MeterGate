"""Stable, secret-free machine-readable catalog contracts."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt

from app.domain.enums import PurchaseType, ServiceType
from app.schemas.common import APIModel, CurrencyCode, JSONObject, OutputContentType

CatalogAmount = Annotated[StrictInt, Field(ge=0)]
CatalogFulfillmentSeconds = Annotated[StrictInt, Field(ge=1, le=86_400)]


class CatalogPricing(APIModel):
    amount: CatalogAmount
    currency: CurrencyCode


class CatalogService(APIModel):
    id: str
    slug: str
    name: str
    description: str
    status: Literal["active"]
    service_type: ServiceType
    purchase_type: PurchaseType
    pricing: CatalogPricing
    input_schema: JSONObject
    output_schema: JSONObject
    output_content_type: OutputContentType
    maximum_fulfillment_seconds: CatalogFulfillmentSeconds
    refund_on_fulfillment_failure: StrictBool


class CatalogMerchantReference(APIModel):
    id: str
    slug: str
    name: str
    description: str


class CatalogMerchant(CatalogMerchantReference):
    services: list[CatalogService]


class CatalogResponse(APIModel):
    version: Literal["1"] = "1"
    generated_at: datetime
    merchants: list[CatalogMerchant]


class CatalogServiceDetail(APIModel):
    version: Literal["1"] = "1"
    generated_at: datetime
    merchant: CatalogMerchantReference
    service: CatalogService
