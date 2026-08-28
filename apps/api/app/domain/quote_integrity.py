"""Pure verification of persisted immutable quote material."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from hmac import compare_digest
from typing import Any

from app.domain.canonical_json import CanonicalJSONError
from app.domain.enums import PurchaseType, ServiceType
from app.domain.hashing import (
    MAX_CANONICAL_INTEGER,
    QuoteIntegrityFields,
    recompute_quote_integrity,
)
from app.domain.integrity import IntegrityStructureError

_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_SNAPSHOT_KEYS = {
    "version",
    "json_schema_dialect",
    "merchant",
    "service",
    "pricing",
    "fulfillment",
}
_MERCHANT_KEYS = {"id", "slug", "name"}
_SERVICE_KEYS = {
    "id",
    "slug",
    "name",
    "service_type",
    "input_schema",
    "output_schema",
    "output_content_type",
}
_PRICING_KEYS = {"amount", "currency", "purchase_type"}
_FULFILLMENT_KEYS = {"maximum_seconds", "refund_on_failure"}


@dataclass(frozen=True, slots=True)
class QuoteIntegrityVerification:
    hash_matches: bool
    service_type: ServiceType


def verify_quote_integrity(quote: QuoteIntegrityFields) -> QuoteIntegrityVerification:
    """Verify quote structure, redundant snapshot terms, and both hashes."""
    try:
        service_type, snapshot_matches = _validate_quote_structure(quote)
        integrity = recompute_quote_integrity(quote)
        input_matches = compare_digest(integrity.input_hash, quote.input_hash)
        quote_matches = compare_digest(integrity.quote_hash, quote.quote_hash)
    except (CanonicalJSONError, KeyError, TypeError, ValueError) as error:
        raise IntegrityStructureError("quote") from error
    return QuoteIntegrityVerification(
        hash_matches=input_matches and quote_matches and snapshot_matches,
        service_type=service_type,
    )


def _validate_quote_structure(quote: QuoteIntegrityFields) -> tuple[ServiceType, bool]:
    if (
        not isinstance(quote.id, str)
        or not isinstance(quote.merchant_id, str)
        or not isinstance(quote.service_id, str)
        or not isinstance(quote.input_hash, str)
        or _HASH_PATTERN.fullmatch(quote.input_hash) is None
        or not isinstance(quote.quote_hash, str)
        or _HASH_PATTERN.fullmatch(quote.quote_hash) is None
        or type(quote.amount) is not int
        or not 0 <= quote.amount <= MAX_CANONICAL_INTEGER
        or not isinstance(quote.currency, str)
        or _CURRENCY_PATTERN.fullmatch(quote.currency) is None
        or type(quote.maximum_fulfillment_seconds) is not int
        or not 1 <= quote.maximum_fulfillment_seconds <= 86_400
        or type(quote.refund_on_fulfillment_failure) is not bool
        or not _aware(quote.issued_at)
        or not _aware(quote.expires_at)
        or quote.expires_at <= quote.issued_at
    ):
        raise ValueError("Invalid quote fields")

    purchase_type = PurchaseType(quote.purchase_type)
    snapshot = quote.service_snapshot
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_KEYS:
        raise ValueError("Invalid quote snapshot")
    if (
        snapshot["version"] != "1"
        or snapshot["json_schema_dialect"] != "https://json-schema.org/draft/2020-12/schema"
    ):
        raise ValueError("Unsupported quote snapshot version")

    merchant = _required_object(snapshot["merchant"], _MERCHANT_KEYS)
    service = _required_object(snapshot["service"], _SERVICE_KEYS)
    pricing = _required_object(snapshot["pricing"], _PRICING_KEYS)
    fulfillment = _required_object(snapshot["fulfillment"], _FULFILLMENT_KEYS)
    service_type = ServiceType(service["service_type"])

    if not all(isinstance(merchant[field], str) for field in _MERCHANT_KEYS) or not all(
        isinstance(service[field], str) for field in {"id", "slug", "name", "output_content_type"}
    ):
        raise ValueError("Invalid quote snapshot strings")
    if not isinstance(service["input_schema"], dict) or not isinstance(
        service["output_schema"], dict
    ):
        raise ValueError("Invalid quote snapshot schemas")
    if (
        type(pricing["amount"]) is not int
        or not 0 <= pricing["amount"] <= MAX_CANONICAL_INTEGER
        or not isinstance(pricing["currency"], str)
        or _CURRENCY_PATTERN.fullmatch(pricing["currency"]) is None
        or type(fulfillment["maximum_seconds"]) is not int
        or not 1 <= fulfillment["maximum_seconds"] <= 86_400
        or type(fulfillment["refund_on_failure"]) is not bool
    ):
        raise ValueError("Invalid quote snapshot terms")
    snapshot_purchase_type = PurchaseType(pricing["purchase_type"])
    snapshot_matches = (
        merchant["id"] == quote.merchant_id
        and service["id"] == quote.service_id
        and pricing["amount"] == quote.amount
        and pricing["currency"] == quote.currency
        and snapshot_purchase_type is purchase_type
        and fulfillment["maximum_seconds"] == quote.maximum_fulfillment_seconds
        and fulfillment["refund_on_failure"] is quote.refund_on_fulfillment_failure
    )
    return service_type, snapshot_matches


def _required_object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid quote snapshot object")
    return value


def _aware(value: Any) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )
