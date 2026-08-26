"""RFC 8785 integrity hashing for immutable paid entitlements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.domain.enums import PurchaseType
from app.domain.hashing import canonical_utc_datetime, sha256_json

ENTITLEMENT_HASH_PAYLOAD_VERSION = "1"


class EntitlementIntegrityFields(Protocol):
    id: str
    account_id: str
    transaction_id: str
    payment_binding_hash: str
    payment_reverification_event_id: str
    payment_reverification_revision: int
    provider_order_id: str
    provider_payment_id: str
    authorization_id: str
    authorization_hash: str
    evaluation_id: str
    policy_id: str
    policy_hash: str
    quote_id: str
    quote_hash: str
    merchant_id: str
    service_id: str
    input: Any
    input_hash: str
    amount: int
    currency: str
    purchase_type: PurchaseType
    maximum_executions: int
    issued_at: datetime
    expires_at: datetime
    entitlement_version: str
    entitlement_hash: str


@dataclass(frozen=True, slots=True)
class EntitlementIntegrity:
    input_hash: str
    entitlement_hash: str


def build_entitlement_hash_payload(
    *,
    entitlement_id: str,
    account_id: str,
    transaction_id: str,
    payment_binding_hash: str,
    payment_reverification_event_id: str,
    payment_reverification_revision: int,
    provider_order_id: str,
    provider_payment_id: str,
    authorization_id: str,
    authorization_hash: str,
    evaluation_id: str,
    policy_id: str,
    policy_hash: str,
    quote_id: str,
    quote_hash: str,
    merchant_id: str,
    service_id: str,
    input_value: Any,
    input_hash: str,
    amount: int,
    currency: str,
    purchase_type: PurchaseType | str,
    maximum_executions: int,
    issued_at: datetime,
    expires_at: datetime,
    entitlement_version: str = ENTITLEMENT_HASH_PAYLOAD_VERSION,
) -> dict[str, Any]:
    """Build the complete versioned evidence payload bound by an entitlement hash."""
    purchase_type_value = (
        purchase_type.value if isinstance(purchase_type, PurchaseType) else purchase_type
    )
    return {
        "version": entitlement_version,
        "entitlement_id": entitlement_id,
        "account_id": account_id,
        "transaction": {
            "id": transaction_id,
            "payment_binding_hash": payment_binding_hash,
            "reverification_event_id": payment_reverification_event_id,
            "reverification_revision": payment_reverification_revision,
            "provider_order_id": provider_order_id,
            "provider_payment_id": provider_payment_id,
        },
        "authorization": {"id": authorization_id, "hash": authorization_hash},
        "evaluation_id": evaluation_id,
        "policy": {"id": policy_id, "hash": policy_hash},
        "quote": {"id": quote_id, "hash": quote_hash},
        "merchant_id": merchant_id,
        "service_id": service_id,
        "input": input_value,
        "input_hash": input_hash,
        "pricing": {
            "amount": amount,
            "currency": currency,
            "purchase_type": purchase_type_value,
        },
        "maximum_executions": maximum_executions,
        "issued_at": canonical_utc_datetime(issued_at),
        "expires_at": canonical_utc_datetime(expires_at),
    }


def calculate_entitlement_hash(**fields: Any) -> str:
    return sha256_json(build_entitlement_hash_payload(**fields))


def recompute_entitlement_integrity(
    entitlement: EntitlementIntegrityFields,
) -> EntitlementIntegrity:
    input_hash = sha256_json(entitlement.input)
    entitlement_hash = calculate_entitlement_hash(
        entitlement_id=entitlement.id,
        account_id=entitlement.account_id,
        transaction_id=entitlement.transaction_id,
        payment_binding_hash=entitlement.payment_binding_hash,
        payment_reverification_event_id=entitlement.payment_reverification_event_id,
        payment_reverification_revision=entitlement.payment_reverification_revision,
        provider_order_id=entitlement.provider_order_id,
        provider_payment_id=entitlement.provider_payment_id,
        authorization_id=entitlement.authorization_id,
        authorization_hash=entitlement.authorization_hash,
        evaluation_id=entitlement.evaluation_id,
        policy_id=entitlement.policy_id,
        policy_hash=entitlement.policy_hash,
        quote_id=entitlement.quote_id,
        quote_hash=entitlement.quote_hash,
        merchant_id=entitlement.merchant_id,
        service_id=entitlement.service_id,
        input_value=entitlement.input,
        input_hash=entitlement.input_hash,
        amount=entitlement.amount,
        currency=entitlement.currency,
        purchase_type=entitlement.purchase_type,
        maximum_executions=entitlement.maximum_executions,
        issued_at=entitlement.issued_at,
        expires_at=entitlement.expires_at,
        entitlement_version=entitlement.entitlement_version,
    )
    return EntitlementIntegrity(input_hash=input_hash, entitlement_hash=entitlement_hash)
