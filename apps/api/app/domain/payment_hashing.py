"""RFC 8785 integrity binding for one authorization-backed payment transaction."""

from __future__ import annotations

from typing import Any, Protocol

from app.domain.enums import PaymentProvider, PurchaseType
from app.domain.hashing import sha256_json

PAYMENT_BINDING_VERSION = "1"


class PaymentBindingFields(Protocol):
    """Persisted immutable fields required to recompute a payment binding."""

    id: str
    account_id: str
    authorization_id: str
    authorization_hash: str
    evaluation_id: str
    policy_id: str
    policy_hash: str
    quote_id: str
    quote_hash: str
    merchant_id: str
    service_id: str
    amount: int
    currency: str
    purchase_type: PurchaseType
    provider: PaymentProvider
    provider_receipt: str
    payment_binding_version: str
    payment_binding_hash: str


def build_payment_binding_payload(
    *,
    transaction_id: str,
    payment_binding_version: str,
    account_id: str,
    authorization_id: str,
    authorization_hash: str,
    evaluation_id: str,
    policy_id: str,
    policy_hash: str,
    quote_id: str,
    quote_hash: str,
    merchant_id: str,
    service_id: str,
    amount: int,
    currency: str,
    purchase_type: PurchaseType | str,
    provider: PaymentProvider | str,
    provider_receipt: str,
) -> dict[str, Any]:
    """Build the exact versioned intent handed to the payment provider."""
    purchase_type_value = (
        purchase_type.value if isinstance(purchase_type, PurchaseType) else purchase_type
    )
    provider_value = provider.value if isinstance(provider, PaymentProvider) else provider
    return {
        "payment_binding_version": payment_binding_version,
        "transaction_id": transaction_id,
        "account_id": account_id,
        "authorization": {
            "id": authorization_id,
            "hash": authorization_hash,
        },
        "evaluation_id": evaluation_id,
        "policy": {"id": policy_id, "hash": policy_hash},
        "quote": {"id": quote_id, "hash": quote_hash},
        "merchant_id": merchant_id,
        "service_id": service_id,
        "pricing": {
            "amount": amount,
            "currency": currency,
            "purchase_type": purchase_type_value,
        },
        "provider": provider_value,
        "provider_receipt": provider_receipt,
    }


def calculate_payment_binding_hash(**fields: Any) -> str:
    """Hash an explicit payment binding using the repository canonicalizer."""
    return sha256_json(build_payment_binding_payload(**fields))


def recompute_payment_binding_hash(transaction: PaymentBindingFields) -> str:
    """Recompute a payment fingerprint from persisted immutable fields."""
    return calculate_payment_binding_hash(
        transaction_id=transaction.id,
        payment_binding_version=transaction.payment_binding_version,
        account_id=transaction.account_id,
        authorization_id=transaction.authorization_id,
        authorization_hash=transaction.authorization_hash,
        evaluation_id=transaction.evaluation_id,
        policy_id=transaction.policy_id,
        policy_hash=transaction.policy_hash,
        quote_id=transaction.quote_id,
        quote_hash=transaction.quote_hash,
        merchant_id=transaction.merchant_id,
        service_id=transaction.service_id,
        amount=transaction.amount,
        currency=transaction.currency,
        purchase_type=transaction.purchase_type,
        provider=transaction.provider,
        provider_receipt=transaction.provider_receipt,
    )
