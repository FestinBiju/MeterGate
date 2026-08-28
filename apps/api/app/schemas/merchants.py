"""Typed management contracts for merchants."""

from datetime import datetime
from typing import Any

from pydantic import model_validator

from app.domain.enums import MerchantStatus
from app.schemas.common import APIModel, Description, Name, ORMResponseModel, Slug
from app.schemas.common import reject_empty_or_null_patch as validate_patch_document


class MerchantCreate(APIModel):
    slug: Slug
    name: Name
    description: Description
    status: MerchantStatus = MerchantStatus.ACTIVE


class MerchantPatch(APIModel):
    slug: Slug | None = None
    name: Name | None = None
    description: Description | None = None
    status: MerchantStatus | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_empty_or_null_patch(cls, data: Any) -> Any:
        return validate_patch_document(data)


class MerchantResponse(ORMResponseModel):
    id: str
    slug: str
    name: str
    description: str
    status: MerchantStatus
    created_at: datetime
    updated_at: datetime
