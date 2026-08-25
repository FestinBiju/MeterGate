"""Reusable SHA-256 integrity primitives for immutable commerce records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol

from app.domain.canonical_json import canonical_json_bytes
from app.domain.enums import PurchaseType

HASH_PREFIX = "sha256:"
QUOTE_HASH_PAYLOAD_VERSION = "1"
MAX_CANONICAL_INTEGER = (1 << 53) - 1


class QuoteIntegrityFields(Protocol):
    """Persisted fields required to recompute quote integrity."""

    id: str
    merchant_id: str
    service_id: str
    service_snapshot: dict[str, Any]
    input: Any
    input_hash: str
    amount: int
    currency: str
    purchase_type: PurchaseType
    maximum_fulfillment_seconds: int
    refund_on_fulfillment_failure: bool
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class QuoteIntegrity:
    input_hash: str
    quote_hash: str


def sha256_bytes(value: bytes) -> str:
    """Hash bytes using the stable, self-describing public digest format."""
    return f"{HASH_PREFIX}{sha256(value).hexdigest()}"


def sha256_json(value: Any) -> str:
    """Hash the RFC 8785 representation of a JSON value."""
    return sha256_bytes(canonical_json_bytes(value))


def canonical_utc_datetime(value: datetime) -> str:
    """Serialize an aware timestamp in one fixed UTC representation."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Integrity timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_quote_hash_payload(
    *,
    quote_id: str,
    merchant_id: str,
    service_id: str,
    service_snapshot: dict[str, Any],
    input_value: Any,
    input_hash: str,
    amount: int,
    currency: str,
    purchase_type: PurchaseType | str,
    maximum_fulfillment_seconds: int,
    refund_on_fulfillment_failure: bool,
    issued_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """Build the versioned immutable material bound by a quote hash."""
    purchase_type_value = (
        purchase_type.value if isinstance(purchase_type, PurchaseType) else purchase_type
    )
    return {
        "version": QUOTE_HASH_PAYLOAD_VERSION,
        "quote_id": quote_id,
        "merchant_id": merchant_id,
        "service_id": service_id,
        "service_snapshot": service_snapshot,
        "input": input_value,
        "input_hash": input_hash,
        "pricing": {
            "amount": amount,
            "currency": currency,
            "purchase_type": purchase_type_value,
        },
        "fulfillment": {
            "maximum_seconds": maximum_fulfillment_seconds,
            "refund_on_failure": refund_on_fulfillment_failure,
        },
        "issued_at": canonical_utc_datetime(issued_at),
        "expires_at": canonical_utc_datetime(expires_at),
    }


def calculate_quote_hash(**fields: Any) -> str:
    """Hash a quote payload assembled from explicit immutable fields."""
    return sha256_json(build_quote_hash_payload(**fields))


def recompute_quote_integrity(quote: QuoteIntegrityFields) -> QuoteIntegrity:
    """Recompute both hashes exclusively from persisted immutable fields."""
    actual_input_hash = sha256_json(quote.input)
    actual_quote_hash = calculate_quote_hash(
        quote_id=quote.id,
        merchant_id=quote.merchant_id,
        service_id=quote.service_id,
        service_snapshot=quote.service_snapshot,
        input_value=quote.input,
        input_hash=quote.input_hash,
        amount=quote.amount,
        currency=quote.currency,
        purchase_type=quote.purchase_type,
        maximum_fulfillment_seconds=quote.maximum_fulfillment_seconds,
        refund_on_fulfillment_failure=quote.refund_on_fulfillment_failure,
        issued_at=quote.issued_at,
        expires_at=quote.expires_at,
    )
    return QuoteIntegrity(input_hash=actual_input_hash, quote_hash=actual_quote_hash)
