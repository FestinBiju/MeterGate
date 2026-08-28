"""RFC 8785 integrity bindings for trusted human purchase approval."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.domain.enums import PurchaseType
from app.domain.hashing import canonical_utc_datetime, sha256_json
from app.domain.policy_engine import PolicyRule

APPROVAL_REVIEW_VERSION = "1"
AUTHORIZATION_VERSION = "1"


def review_policy_checks(
    checks: list[dict[str, Any]],
    *,
    currency: str,
    merchant_name: str,
    service_name: str,
) -> list[dict[str, Any]]:
    """Attach concise, server-derived human explanations to verified checks."""
    explanations: dict[PolicyRule, Any] = {
        PolicyRule.POLICY_INTEGRITY: lambda _: "Policy integrity verified",
        PolicyRule.QUOTE_INTEGRITY: lambda _: "Quote integrity verified",
        PolicyRule.POLICY_FRESHNESS: lambda _: "Policy is active",
        PolicyRule.QUOTE_FRESHNESS: lambda _: "Quote is active",
        PolicyRule.MAXIMUM_AMOUNT: lambda details: (
            f"{details['actual']} ≤ {details['maximum']} {currency} minor units"
        ),
        PolicyRule.CURRENCY: lambda details: f"{details['actual']} allowed",
        PolicyRule.MERCHANT: lambda _: f"Merchant {merchant_name} allowed",
        PolicyRule.SERVICE: lambda _: f"Service {service_name} allowed",
        PolicyRule.SERVICE_TYPE: lambda details: (
            f"Service type {str(details['actual']).replace('_', ' ')} allowed"
        ),
        PolicyRule.PURCHASE_TYPE: lambda details: (
            f"Purchase type {str(details['actual']).replace('_', ' ')} allowed"
        ),
    }
    review_checks: list[dict[str, Any]] = []
    for check in checks:
        rule = PolicyRule(check["rule"])
        details = check["details"]
        if not isinstance(details, dict):
            raise ValueError("Policy check details must be an object")
        review_checks.append(
            {
                "rule": rule.value,
                "result": check["result"],
                "reason_code": check["reason_code"],
                "details": details,
                "explanation": explanations[rule](details),
            }
        )
    return review_checks


class AuthorizationIntegrityFields(Protocol):
    """Persisted fields required to recompute authorization integrity."""

    id: str
    approval_identity_id: str
    passkey_credential_id: str
    evaluation_id: str
    policy_id: str
    policy_hash: str
    quote_id: str
    quote_hash: str
    merchant_id: str
    service_id: str
    subject_ref: str
    amount: int
    currency: str
    purchase_type: PurchaseType
    review_hash: str
    challenge_hash: str
    authorized_at: datetime
    expires_at: datetime
    authorization_version: str
    authorization_hash: str


def build_approval_review_payload(
    *,
    evaluation_id: str,
    evaluation_version: str,
    policy_id: str,
    policy_hash: str,
    quote_id: str,
    quote_hash: str,
    subject_ref: str,
    approval_identity_id: str,
    merchant_id: str,
    merchant_name: str,
    service_id: str,
    service_name: str,
    amount: int,
    currency: str,
    purchase_type: PurchaseType | str,
    quote_expires_at: datetime,
    policy_expires_at: datetime,
    policy_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the exact versioned payload displayed and bound to a challenge."""
    purchase_type_value = (
        purchase_type.value if isinstance(purchase_type, PurchaseType) else purchase_type
    )
    return {
        "review_version": APPROVAL_REVIEW_VERSION,
        "evaluation_id": evaluation_id,
        "evaluation_version": evaluation_version,
        "policy_id": policy_id,
        "policy_hash": policy_hash,
        "quote_id": quote_id,
        "quote_hash": quote_hash,
        "subject_ref": subject_ref,
        "approval_identity_id": approval_identity_id,
        "merchant": {"id": merchant_id, "name": merchant_name},
        "service": {"id": service_id, "name": service_name},
        "amount": amount,
        "currency": currency,
        "purchase_type": purchase_type_value,
        "quote_expires_at": canonical_utc_datetime(quote_expires_at),
        "policy_expires_at": canonical_utc_datetime(policy_expires_at),
        "policy_checks": policy_checks,
    }


def calculate_approval_review_hash(**fields: Any) -> str:
    """Hash an approval review using the repository RFC 8785 canonicalizer."""
    return sha256_json(build_approval_review_payload(**fields))


def build_authorization_hash_payload(
    *,
    authorization_id: str,
    authorization_version: str,
    approval_identity_id: str,
    passkey_credential_id: str,
    subject_ref: str,
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
    review_hash: str,
    challenge_hash: str,
    authorized_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """Build immutable material that identifies exactly what was authorized."""
    purchase_type_value = (
        purchase_type.value if isinstance(purchase_type, PurchaseType) else purchase_type
    )
    return {
        "authorization_id": authorization_id,
        "authorization_version": authorization_version,
        "approval_identity_id": approval_identity_id,
        "passkey_credential_id": passkey_credential_id,
        "subject_ref": subject_ref,
        "evaluation_id": evaluation_id,
        "policy": {"id": policy_id, "hash": policy_hash},
        "quote": {"id": quote_id, "hash": quote_hash},
        "merchant_id": merchant_id,
        "service_id": service_id,
        "amount": amount,
        "currency": currency,
        "purchase_type": purchase_type_value,
        "review_hash": review_hash,
        "challenge_hash": challenge_hash,
        "authorized_at": canonical_utc_datetime(authorized_at),
        "expires_at": canonical_utc_datetime(expires_at),
    }


def calculate_authorization_hash(**fields: Any) -> str:
    """Hash an explicit authorization payload assembled by the server."""
    return sha256_json(build_authorization_hash_payload(**fields))


def recompute_authorization_hash(authorization: AuthorizationIntegrityFields) -> str:
    """Recompute an authorization fingerprint from persisted immutable fields."""
    return calculate_authorization_hash(
        authorization_id=authorization.id,
        authorization_version=authorization.authorization_version,
        approval_identity_id=authorization.approval_identity_id,
        passkey_credential_id=authorization.passkey_credential_id,
        subject_ref=authorization.subject_ref,
        evaluation_id=authorization.evaluation_id,
        policy_id=authorization.policy_id,
        policy_hash=authorization.policy_hash,
        quote_id=authorization.quote_id,
        quote_hash=authorization.quote_hash,
        merchant_id=authorization.merchant_id,
        service_id=authorization.service_id,
        amount=authorization.amount,
        currency=authorization.currency,
        purchase_type=authorization.purchase_type,
        review_hash=authorization.review_hash,
        challenge_hash=authorization.challenge_hash,
        authorized_at=authorization.authorized_at,
        expires_at=authorization.expires_at,
    )
