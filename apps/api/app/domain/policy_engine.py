"""Pure, deterministic evaluation of immutable quotes against buyer policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.domain.enums import PolicyDecision, PurchaseType
from app.domain.hashing import QuoteIntegrityFields, canonical_utc_datetime
from app.domain.integrity import IntegrityStructureError
from app.domain.policy_hashing import (
    BuyerPolicyIntegrityFields,
    normalize_allowlist,
    verify_policy_integrity,
)
from app.domain.quote_integrity import verify_quote_integrity

POLICY_EVALUATION_VERSION = "1"


class PolicyRule(StrEnum):
    POLICY_INTEGRITY = "POLICY_INTEGRITY"
    QUOTE_INTEGRITY = "QUOTE_INTEGRITY"
    POLICY_FRESHNESS = "POLICY_FRESHNESS"
    QUOTE_FRESHNESS = "QUOTE_FRESHNESS"
    MAXIMUM_AMOUNT = "MAXIMUM_AMOUNT"
    CURRENCY = "CURRENCY"
    MERCHANT = "MERCHANT"
    SERVICE = "SERVICE"
    SERVICE_TYPE = "SERVICE_TYPE"
    PURCHASE_TYPE = "PURCHASE_TYPE"


class PolicyCheckResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class PolicyReasonCode(StrEnum):
    ALLOW_POLICY_SATISFIED = "ALLOW_POLICY_SATISFIED"
    PASS_POLICY_INTEGRITY_VERIFIED = "PASS_POLICY_INTEGRITY_VERIFIED"
    INTEGRITY_POLICY_HASH_MISMATCH = "INTEGRITY_POLICY_HASH_MISMATCH"
    PASS_QUOTE_INTEGRITY_VERIFIED = "PASS_QUOTE_INTEGRITY_VERIFIED"
    INTEGRITY_QUOTE_HASH_MISMATCH = "INTEGRITY_QUOTE_HASH_MISMATCH"
    PASS_POLICY_ACTIVE = "PASS_POLICY_ACTIVE"
    DENY_POLICY_EXPIRED = "DENY_POLICY_EXPIRED"
    PASS_QUOTE_ACTIVE = "PASS_QUOTE_ACTIVE"
    DENY_QUOTE_EXPIRED = "DENY_QUOTE_EXPIRED"
    PASS_AMOUNT_WITHIN_LIMIT = "PASS_AMOUNT_WITHIN_LIMIT"
    DENY_AMOUNT_EXCEEDS_LIMIT = "DENY_AMOUNT_EXCEEDS_LIMIT"
    PASS_CURRENCY_ALLOWED = "PASS_CURRENCY_ALLOWED"
    DENY_CURRENCY_NOT_ALLOWED = "DENY_CURRENCY_NOT_ALLOWED"
    PASS_MERCHANT_ALLOWED = "PASS_MERCHANT_ALLOWED"
    DENY_MERCHANT_NOT_ALLOWED = "DENY_MERCHANT_NOT_ALLOWED"
    PASS_SERVICE_ALLOWED = "PASS_SERVICE_ALLOWED"
    DENY_SERVICE_NOT_ALLOWED = "DENY_SERVICE_NOT_ALLOWED"
    PASS_SERVICE_TYPE_ALLOWED = "PASS_SERVICE_TYPE_ALLOWED"
    DENY_SERVICE_TYPE_NOT_ALLOWED = "DENY_SERVICE_TYPE_NOT_ALLOWED"
    PASS_PURCHASE_TYPE_ALLOWED = "PASS_PURCHASE_TYPE_ALLOWED"
    DENY_PURCHASE_TYPE_NOT_ALLOWED = "DENY_PURCHASE_TYPE_NOT_ALLOWED"


@dataclass(frozen=True, slots=True)
class PolicyCheck:
    rule: PolicyRule
    result: PolicyCheckResult
    reason_code: PolicyReasonCode
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.value,
            "result": self.result.value,
            "reason_code": self.reason_code.value,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class PolicyEvaluationResult:
    decision: PolicyDecision
    reason_codes: tuple[PolicyReasonCode, ...]
    checks: tuple[PolicyCheck, ...]


def evaluate_policy(
    policy: BuyerPolicyIntegrityFields,
    quote: QuoteIntegrityFields,
    now: datetime,
) -> PolicyEvaluationResult:
    """Evaluate every safe independent check in one intentional order."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Policy evaluation time must be timezone-aware")

    policy_integrity = verify_policy_integrity(policy)
    quote_integrity = verify_quote_integrity(quote)
    checks = [
        _integrity_check(
            PolicyRule.POLICY_INTEGRITY,
            policy_integrity.hash_matches,
            PolicyReasonCode.PASS_POLICY_INTEGRITY_VERIFIED,
            PolicyReasonCode.INTEGRITY_POLICY_HASH_MISMATCH,
        ),
        _integrity_check(
            PolicyRule.QUOTE_INTEGRITY,
            quote_integrity.hash_matches,
            PolicyReasonCode.PASS_QUOTE_INTEGRITY_VERIFIED,
            PolicyReasonCode.INTEGRITY_QUOTE_HASH_MISMATCH,
        ),
    ]
    if now < policy.issued_at:
        raise IntegrityStructureError("policy")
    if now < quote.issued_at:
        raise IntegrityStructureError("quote")
    if not policy_integrity.hash_matches or not quote_integrity.hash_matches:
        return _result(checks)

    evaluated_at = canonical_utc_datetime(now)
    checks.extend(
        (
            _boolean_check(
                PolicyRule.POLICY_FRESHNESS,
                now < policy.expires_at,
                PolicyReasonCode.PASS_POLICY_ACTIVE,
                PolicyReasonCode.DENY_POLICY_EXPIRED,
                {
                    "evaluated_at": evaluated_at,
                    "expires_at": canonical_utc_datetime(policy.expires_at),
                },
            ),
            _boolean_check(
                PolicyRule.QUOTE_FRESHNESS,
                now < quote.expires_at,
                PolicyReasonCode.PASS_QUOTE_ACTIVE,
                PolicyReasonCode.DENY_QUOTE_EXPIRED,
                {
                    "evaluated_at": evaluated_at,
                    "expires_at": canonical_utc_datetime(quote.expires_at),
                },
            ),
            _boolean_check(
                PolicyRule.MAXIMUM_AMOUNT,
                quote.amount <= policy.maximum_amount,
                PolicyReasonCode.PASS_AMOUNT_WITHIN_LIMIT,
                PolicyReasonCode.DENY_AMOUNT_EXCEEDS_LIMIT,
                {"actual": quote.amount, "maximum": policy.maximum_amount},
            ),
            _allowlist_check(
                PolicyRule.CURRENCY,
                quote.currency,
                policy.allowed_currencies,
                PolicyReasonCode.PASS_CURRENCY_ALLOWED,
                PolicyReasonCode.DENY_CURRENCY_NOT_ALLOWED,
            ),
            _allowlist_check(
                PolicyRule.MERCHANT,
                quote.merchant_id,
                policy.allowed_merchant_ids,
                PolicyReasonCode.PASS_MERCHANT_ALLOWED,
                PolicyReasonCode.DENY_MERCHANT_NOT_ALLOWED,
            ),
            _allowlist_check(
                PolicyRule.SERVICE,
                quote.service_id,
                policy.allowed_service_ids,
                PolicyReasonCode.PASS_SERVICE_ALLOWED,
                PolicyReasonCode.DENY_SERVICE_NOT_ALLOWED,
            ),
            _allowlist_check(
                PolicyRule.SERVICE_TYPE,
                quote_integrity.service_type.value,
                policy.allowed_service_types,
                PolicyReasonCode.PASS_SERVICE_TYPE_ALLOWED,
                PolicyReasonCode.DENY_SERVICE_TYPE_NOT_ALLOWED,
            ),
            _allowlist_check(
                PolicyRule.PURCHASE_TYPE,
                PurchaseType(quote.purchase_type).value,
                policy.allowed_purchase_types,
                PolicyReasonCode.PASS_PURCHASE_TYPE_ALLOWED,
                PolicyReasonCode.DENY_PURCHASE_TYPE_NOT_ALLOWED,
            ),
        )
    )
    return _result(checks)


def _integrity_check(
    rule: PolicyRule,
    passed: bool,
    pass_code: PolicyReasonCode,
    fail_code: PolicyReasonCode,
) -> PolicyCheck:
    return _boolean_check(rule, passed, pass_code, fail_code, {})


def _allowlist_check(
    rule: PolicyRule,
    actual: str,
    configured: list[str] | None,
    pass_code: PolicyReasonCode,
    fail_code: PolicyReasonCode,
) -> PolicyCheck:
    allowed = normalize_allowlist(configured)
    return _boolean_check(
        rule,
        allowed is None or actual in allowed,
        pass_code,
        fail_code,
        {"actual": actual, "allowed": allowed},
    )


def _boolean_check(
    rule: PolicyRule,
    passed: bool,
    pass_code: PolicyReasonCode,
    fail_code: PolicyReasonCode,
    details: dict[str, Any],
) -> PolicyCheck:
    return PolicyCheck(
        rule=rule,
        result=PolicyCheckResult.PASS if passed else PolicyCheckResult.FAIL,
        reason_code=pass_code if passed else fail_code,
        details=details,
    )


def _result(checks: list[PolicyCheck]) -> PolicyEvaluationResult:
    failures = tuple(
        check.reason_code for check in checks if check.result is PolicyCheckResult.FAIL
    )
    if failures:
        return PolicyEvaluationResult(
            decision=PolicyDecision.DENY,
            reason_codes=failures,
            checks=tuple(checks),
        )
    return PolicyEvaluationResult(
        decision=PolicyDecision.ALLOW,
        reason_codes=(PolicyReasonCode.ALLOW_POLICY_SATISFIED,),
        checks=tuple(checks),
    )
