"""Normalization and RFC 8785 integrity binding for immutable buyer policies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from hmac import compare_digest
from typing import Any, Protocol

from app.domain.canonical_json import CanonicalJSONError
from app.domain.enums import PurchaseType, ServiceType
from app.domain.hashing import MAX_CANONICAL_INTEGER, canonical_utc_datetime, sha256_json
from app.domain.integrity import IntegrityStructureError

POLICY_VERSION = "1"
MAX_POLICY_ALLOWLIST_ITEMS = 100
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_POLICY_ID_PATTERN = re.compile(r"^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_MERCHANT_ID_PATTERN = re.compile(r"^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_SERVICE_ID_PATTERN = re.compile(r"^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


class BuyerPolicyIntegrityFields(Protocol):
    id: str
    subject_ref: str
    maximum_amount: int
    allowed_currencies: list[str] | None
    allowed_merchant_ids: list[str] | None
    allowed_service_ids: list[str] | None
    allowed_service_types: list[str] | None
    allowed_purchase_types: list[str] | None
    issued_at: datetime
    expires_at: datetime
    policy_version: str
    policy_hash: str


@dataclass(frozen=True, slots=True)
class PolicyIntegrityVerification:
    hash_matches: bool


def normalize_allowlist(values: list[str] | None) -> list[str] | None:
    """Return the one persisted representation of an optional set-like list."""
    if values is None:
        return None
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("Allowlist must be a list of strings")
    normalized = [str(value) for value in values]
    if (
        not normalized
        or len(normalized) > MAX_POLICY_ALLOWLIST_ITEMS
        or len(normalized) != len(set(normalized))
    ):
        raise ValueError("Allowlist must be nonempty, bounded, and contain unique values")
    return sorted(normalized)


def build_policy_hash_payload(
    *,
    policy_id: str,
    subject_ref: str,
    maximum_amount: int,
    allowed_currencies: list[str] | None,
    allowed_merchant_ids: list[str] | None,
    allowed_service_ids: list[str] | None,
    allowed_service_types: list[str] | None,
    allowed_purchase_types: list[str] | None,
    issued_at: datetime,
    expires_at: datetime,
    policy_version: str,
) -> dict[str, Any]:
    """Build normalized immutable material for a buyer-policy hash."""
    return {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "subject_ref": subject_ref,
        "constraints": {
            "maximum_amount": maximum_amount,
            "allowed_currencies": normalize_allowlist(allowed_currencies),
            "allowed_merchant_ids": normalize_allowlist(allowed_merchant_ids),
            "allowed_service_ids": normalize_allowlist(allowed_service_ids),
            "allowed_service_types": normalize_allowlist(allowed_service_types),
            "allowed_purchase_types": normalize_allowlist(allowed_purchase_types),
        },
        "issued_at": canonical_utc_datetime(issued_at),
        "expires_at": canonical_utc_datetime(expires_at),
    }


def calculate_policy_hash(**fields: Any) -> str:
    return sha256_json(build_policy_hash_payload(**fields))


def recompute_policy_hash(policy: BuyerPolicyIntegrityFields) -> str:
    return calculate_policy_hash(
        policy_id=policy.id,
        subject_ref=policy.subject_ref,
        maximum_amount=policy.maximum_amount,
        allowed_currencies=policy.allowed_currencies,
        allowed_merchant_ids=policy.allowed_merchant_ids,
        allowed_service_ids=policy.allowed_service_ids,
        allowed_service_types=policy.allowed_service_types,
        allowed_purchase_types=policy.allowed_purchase_types,
        issued_at=policy.issued_at,
        expires_at=policy.expires_at,
        policy_version=policy.policy_version,
    )


def verify_policy_integrity(
    policy: BuyerPolicyIntegrityFields,
) -> PolicyIntegrityVerification:
    try:
        _validate_policy_structure(policy)
        actual_hash = recompute_policy_hash(policy)
        hash_matches = compare_digest(actual_hash, policy.policy_hash)
    except (AttributeError, CanonicalJSONError, TypeError, ValueError) as error:
        raise IntegrityStructureError("policy") from error
    return PolicyIntegrityVerification(hash_matches=hash_matches)


def _validate_policy_structure(policy: BuyerPolicyIntegrityFields) -> None:
    if (
        not isinstance(policy.id, str)
        or _POLICY_ID_PATTERN.fullmatch(policy.id) is None
        or not isinstance(policy.subject_ref, str)
        or not policy.subject_ref.strip()
        or policy.subject_ref != policy.subject_ref.strip()
        or type(policy.maximum_amount) is not int
        or not 0 <= policy.maximum_amount <= MAX_CANONICAL_INTEGER
        or policy.policy_version != POLICY_VERSION
        or not isinstance(policy.policy_hash, str)
        or _HASH_PATTERN.fullmatch(policy.policy_hash) is None
        or not _aware(policy.issued_at)
        or not _aware(policy.expires_at)
        or policy.expires_at <= policy.issued_at
    ):
        raise ValueError("Invalid policy fields")

    _validate_values(policy.allowed_currencies, _CURRENCY_PATTERN)
    _validate_values(policy.allowed_merchant_ids, _MERCHANT_ID_PATTERN)
    _validate_values(policy.allowed_service_ids, _SERVICE_ID_PATTERN)
    _validate_enum_values(policy.allowed_service_types, ServiceType)
    _validate_enum_values(policy.allowed_purchase_types, PurchaseType)


def _validate_values(values: list[str] | None, pattern: re.Pattern[str]) -> None:
    normalized = normalize_allowlist(values)
    if normalized is not None and any(pattern.fullmatch(value) is None for value in normalized):
        raise ValueError("Invalid policy allowlist value")


def _validate_enum_values(values: list[str] | None, enum_type: Any) -> None:
    normalized = normalize_allowlist(values)
    if normalized is not None:
        for value in normalized:
            enum_type(value)


def _aware(value: Any) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )
