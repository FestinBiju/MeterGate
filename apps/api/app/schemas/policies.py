"""Strict public contracts for immutable buyer spending policies."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt, StringConstraints, field_validator

from app.domain.enums import PurchaseType, ServiceType
from app.domain.hashing import MAX_CANONICAL_INTEGER
from app.domain.policy_hashing import MAX_POLICY_ALLOWLIST_ITEMS
from app.schemas.common import APIModel, CurrencyCode

SubjectReference = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
PolicyAmount = Annotated[StrictInt, Field(ge=0, le=MAX_CANONICAL_INTEGER)]
PolicyTTLSeconds = Annotated[StrictInt, Field(ge=1, le=86_400)]
SHA256Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
MerchantId = Annotated[
    str,
    StringConstraints(pattern=r"^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
ServiceId = Annotated[
    str,
    StringConstraints(pattern=r"^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
PolicyId = Annotated[
    str,
    StringConstraints(pattern=r"^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
PolicyEvaluationId = Annotated[
    str,
    StringConstraints(pattern=r"^pye_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
QuoteId = Annotated[
    str,
    StringConstraints(pattern=r"^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]

CurrencyAllowlist = Annotated[
    list[CurrencyCode],
    Field(min_length=1, max_length=MAX_POLICY_ALLOWLIST_ITEMS),
]
MerchantAllowlist = Annotated[
    list[MerchantId],
    Field(min_length=1, max_length=MAX_POLICY_ALLOWLIST_ITEMS),
]
ServiceAllowlist = Annotated[
    list[ServiceId],
    Field(min_length=1, max_length=MAX_POLICY_ALLOWLIST_ITEMS),
]
ServiceTypeAllowlist = Annotated[
    list[ServiceType],
    Field(min_length=1, max_length=MAX_POLICY_ALLOWLIST_ITEMS),
]
PurchaseTypeAllowlist = Annotated[
    list[PurchaseType],
    Field(min_length=1, max_length=MAX_POLICY_ALLOWLIST_ITEMS),
]


class PolicyConstraints(APIModel):
    maximum_amount: PolicyAmount
    allowed_currencies: CurrencyAllowlist | None = None
    allowed_merchant_ids: MerchantAllowlist | None = None
    allowed_service_ids: ServiceAllowlist | None = None
    allowed_service_types: ServiceTypeAllowlist | None = None
    allowed_purchase_types: PurchaseTypeAllowlist | None = None


class BuyerPolicyCreate(PolicyConstraints):
    expires_in_seconds: PolicyTTLSeconds

    @field_validator(
        "allowed_currencies",
        "allowed_merchant_ids",
        "allowed_service_ids",
        "allowed_service_types",
        "allowed_purchase_types",
    )
    @classmethod
    def reject_duplicates_and_sort(
        cls,
        value: list[Any] | None,
    ) -> list[Any] | None:
        if value is None:
            return None
        comparable = [_string_value(item) for item in value]
        if len(comparable) != len(set(comparable)):
            raise ValueError("allowlist values must be unique")
        return sorted(value, key=_string_value)


class BuyerPolicyResponse(APIModel):
    id: PolicyId
    subject_ref: SubjectReference
    constraints: PolicyConstraints
    issued_at: datetime
    expires_at: datetime
    state: Literal["active", "expired"]
    policy_version: Literal["1"]
    policy_hash: SHA256Digest
    created_at: datetime


def _string_value(value: Any) -> str:
    return value.value if isinstance(value, StrEnum) else str(value)
