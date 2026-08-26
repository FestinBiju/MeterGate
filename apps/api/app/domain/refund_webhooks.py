"""Strict normalization for signed Razorpay refund webhook evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.hashing import MAX_CANONICAL_INTEGER
from app.providers import ProviderRefund, validate_provider_event_type
from app.providers.base import validate_provider_order_id, validate_provider_payment_id

RAZORPAY_REFUND_WEBHOOK_EVENTS = frozenset(
    {
        "refund.created",
        "refund.processed",
        "refund.failed",
        "refund.speed_changed",
    }
)


@dataclass(frozen=True, slots=True)
class RefundWebhookEvidence:
    provider_event_type: str
    provider_created_at: datetime
    provider_order_id: str
    payment_amount: int
    payment_amount_refunded: int | None
    refund: ProviderRefund


def parse_refund_webhook(raw_body: bytes) -> RefundWebhookEvidence:
    """Parse only the trust-relevant subset after raw-body HMAC verification."""
    try:
        raw = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Refund webhook JSON is invalid") from error
    if not isinstance(raw, Mapping) or raw.get("entity") != "event":
        raise ValueError("Refund webhook envelope is invalid")
    event_type = raw.get("event")
    validate_provider_event_type(event_type)  # type: ignore[arg-type]
    if event_type not in RAZORPAY_REFUND_WEBHOOK_EVENTS:
        raise ValueError("Webhook is not a supported refund event")
    provider_created_at = _timestamp(raw.get("created_at"), field="event created_at")
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("Refund webhook payload is invalid")
    refund_entity = _entity(payload, "refund")
    payment_entity = _entity(payload, "payment")

    payment_id = payment_entity.get("id")
    provider_order_id = payment_entity.get("order_id")
    validate_provider_payment_id(payment_id)  # type: ignore[arg-type]
    validate_provider_order_id(provider_order_id)  # type: ignore[arg-type]
    refund_payment_id = refund_entity.get("payment_id")
    if refund_payment_id != payment_id:
        raise ValueError("Refund webhook entities reference different payments")

    amount = refund_entity.get("amount")
    payment_amount = payment_entity.get("amount")
    if (
        type(amount) is not int
        or not 1 <= amount <= MAX_CANONICAL_INTEGER
        or type(payment_amount) is not int
        or not amount <= payment_amount <= MAX_CANONICAL_INTEGER
    ):
        raise ValueError("Refund webhook amount is invalid")
    amount_refunded = payment_entity.get("amount_refunded")
    if amount_refunded is not None and (
        type(amount_refunded) is not int or not 0 <= amount_refunded <= payment_amount
    ):
        raise ValueError("Refund webhook cumulative amount is invalid")

    currency = _optional_currency(refund_entity.get("currency"))
    payment_currency = _optional_currency(payment_entity.get("currency"))
    if currency is not None and payment_currency is not None and currency != payment_currency:
        raise ValueError("Refund webhook currency does not match its payment")
    effective_currency = currency or payment_currency
    if effective_currency is None:
        raise ValueError("Refund webhook currency evidence is missing")
    receipt = refund_entity.get("receipt")
    if receipt is not None and not isinstance(receipt, str):
        raise ValueError("Refund webhook receipt is invalid")
    status = refund_entity.get("status")
    refund = ProviderRefund(
        id=refund_entity.get("id"),  # type: ignore[arg-type]
        payment_id=refund_payment_id,  # type: ignore[arg-type]
        amount=amount,
        currency=effective_currency,
        receipt=receipt,
        status=status,  # type: ignore[arg-type]
        created_at=_timestamp(refund_entity.get("created_at"), field="refund created_at"),
    )
    if event_type == "refund.processed" and refund.status != "processed":
        raise ValueError("Processed refund webhook has a non-processed refund")
    if event_type == "refund.failed" and refund.status != "failed":
        raise ValueError("Failed refund webhook has a non-failed refund")
    if (
        refund.status == "processed"
        and amount_refunded is not None
        and amount_refunded < refund.amount
    ):
        raise ValueError("Processed refund exceeds the payment's cumulative refund evidence")
    return RefundWebhookEvidence(
        provider_event_type=event_type,
        provider_created_at=provider_created_at,
        provider_order_id=provider_order_id,  # type: ignore[arg-type]
        payment_amount=payment_amount,
        payment_amount_refunded=amount_refunded,
        refund=refund,
    )


def _entity(payload: Mapping[str, object], name: str) -> Mapping[str, object]:
    wrapper = payload.get(name)
    if not isinstance(wrapper, Mapping) or set(wrapper) != {"entity"}:
        raise ValueError(f"Refund webhook {name} wrapper is invalid")
    entity = wrapper.get("entity")
    if not isinstance(entity, Mapping):
        raise ValueError(f"Refund webhook {name} entity is invalid")
    return entity


def _timestamp(value: object, *, field: str) -> datetime:
    if type(value) is not int or not 0 <= value <= MAX_CANONICAL_INTEGER:
        raise ValueError(f"Refund webhook {field} is invalid")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError) as error:
        raise ValueError(f"Refund webhook {field} is invalid") from error


def _optional_currency(value: object) -> str | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str) or len(value) != 3 or value != value.upper():
        raise ValueError("Refund webhook currency is invalid")
    return value
