"""Pure deterministic policy for quote-authorized fulfillment compensation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from app.domain.enums import (
    CompensationDecisionProvenance,
    CompensationDecisionState,
    CompensationRecommendedAction,
    FulfillmentExecutionState,
    PaymentAttemptStatus,
    PaymentTransactionState,
)
from app.domain.hashing import QuoteIntegrityFields
from app.domain.integrity import IntegrityStructureError
from app.domain.quote_integrity import verify_quote_integrity


class CompensationPolicyReasonCode(StrEnum):
    COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED = "COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED"
    COMPENSATION_MANUAL_REVIEW_REQUIRED = "COMPENSATION_MANUAL_REVIEW_REQUIRED"
    COMPENSATION_NOT_REQUIRED = "COMPENSATION_NOT_REQUIRED"
    COMPENSATION_NOT_ELIGIBLE_PAYMENT_NOT_PAID = "COMPENSATION_NOT_ELIGIBLE_PAYMENT_NOT_PAID"
    COMPENSATION_NOT_ELIGIBLE_CAPTURE_MISSING = "COMPENSATION_NOT_ELIGIBLE_CAPTURE_MISSING"
    COMPENSATION_NOT_ELIGIBLE_VALUE_DELIVERED = "COMPENSATION_NOT_ELIGIBLE_VALUE_DELIVERED"
    COMPENSATION_NOT_ELIGIBLE_EVIDENCE_MISMATCH = "COMPENSATION_NOT_ELIGIBLE_EVIDENCE_MISMATCH"
    COMPENSATION_NOT_ELIGIBLE_QUOTE_INTEGRITY = "COMPENSATION_NOT_ELIGIBLE_QUOTE_INTEGRITY"


class PaymentTransactionCompensationFields(Protocol):
    id: str
    account_id: str
    quote_id: str
    quote_hash: str
    merchant_id: str
    service_id: str
    amount: int
    currency: str
    transaction_state: PaymentTransactionState


class PaymentAttemptCompensationFields(Protocol):
    id: str
    transaction_id: str
    provider_payment_id: str
    amount: int
    currency: str
    provider_status: PaymentAttemptStatus
    captured: bool


class EntitlementCompensationFields(Protocol):
    id: str
    account_id: str
    transaction_id: str
    provider_payment_id: str
    quote_id: str
    quote_hash: str
    merchant_id: str
    service_id: str
    input_hash: str
    amount: int
    currency: str


class FulfillmentCompensationFields(Protocol):
    id: str
    entitlement_id: str
    account_id: str
    transaction_id: str
    merchant_id: str
    service_id: str
    input_hash: str
    execution_state: FulfillmentExecutionState
    compensation_required: bool
    result_content_type: str | None
    result_json: Any | None
    result_hash: str | None
    result_size_bytes: int | None


@dataclass(frozen=True, slots=True)
class CompensationPolicyResult:
    create_case: bool
    recommended_action: CompensationRecommendedAction
    decision_state: CompensationDecisionState | None
    decision_provenance: CompensationDecisionProvenance | None
    approved_refund_amount: int | None
    reason_code: CompensationPolicyReasonCode


def evaluate_compensation_policy(
    *,
    transaction: PaymentTransactionCompensationFields,
    payment_attempt: PaymentAttemptCompensationFields,
    entitlement: EntitlementCompensationFields,
    fulfillment: FulfillmentCompensationFields,
    quote: QuoteIntegrityFields,
) -> CompensationPolicyResult:
    """Recommend remediation using only immutable, server-owned commerce evidence."""
    if PaymentTransactionState(transaction.transaction_state) is not PaymentTransactionState.PAID:
        return _not_eligible(
            CompensationPolicyReasonCode.COMPENSATION_NOT_ELIGIBLE_PAYMENT_NOT_PAID
        )
    if not payment_attempt.captured:
        return _not_eligible(CompensationPolicyReasonCode.COMPENSATION_NOT_ELIGIBLE_CAPTURE_MISSING)
    if (
        FulfillmentExecutionState(fulfillment.execution_state)
        is not FulfillmentExecutionState.PERMANENT_FAILURE
        or not fulfillment.compensation_required
    ):
        return _not_eligible(CompensationPolicyReasonCode.COMPENSATION_NOT_REQUIRED)
    if any(
        value is not None
        for value in (
            fulfillment.result_content_type,
            fulfillment.result_json,
            fulfillment.result_hash,
            fulfillment.result_size_bytes,
        )
    ):
        return _not_eligible(CompensationPolicyReasonCode.COMPENSATION_NOT_ELIGIBLE_VALUE_DELIVERED)

    if not _bindings_match(transaction, payment_attempt, entitlement, fulfillment, quote):
        return _manual_review(
            CompensationPolicyReasonCode.COMPENSATION_NOT_ELIGIBLE_EVIDENCE_MISMATCH
        )

    try:
        quote_integrity = verify_quote_integrity(quote)
    except IntegrityStructureError:
        return _manual_review(
            CompensationPolicyReasonCode.COMPENSATION_NOT_ELIGIBLE_QUOTE_INTEGRITY
        )
    if not quote_integrity.hash_matches:
        return _manual_review(
            CompensationPolicyReasonCode.COMPENSATION_NOT_ELIGIBLE_QUOTE_INTEGRITY
        )
    if PaymentAttemptStatus(payment_attempt.provider_status) is not PaymentAttemptStatus.CAPTURED:
        return _manual_review(
            CompensationPolicyReasonCode.COMPENSATION_NOT_ELIGIBLE_EVIDENCE_MISMATCH
        )
    if not quote.refund_on_fulfillment_failure:
        return _manual_review(CompensationPolicyReasonCode.COMPENSATION_MANUAL_REVIEW_REQUIRED)

    return CompensationPolicyResult(
        create_case=True,
        recommended_action=CompensationRecommendedAction.FULL_REFUND,
        decision_state=CompensationDecisionState.APPROVED,
        decision_provenance=CompensationDecisionProvenance.AUTOMATIC_APPROVED,
        approved_refund_amount=transaction.amount,
        reason_code=(CompensationPolicyReasonCode.COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED),
    )


def _bindings_match(
    transaction: PaymentTransactionCompensationFields,
    payment_attempt: PaymentAttemptCompensationFields,
    entitlement: EntitlementCompensationFields,
    fulfillment: FulfillmentCompensationFields,
    quote: QuoteIntegrityFields,
) -> bool:
    return (
        payment_attempt.transaction_id == transaction.id
        and payment_attempt.amount == transaction.amount
        and payment_attempt.currency == transaction.currency
        and entitlement.account_id == transaction.account_id
        and entitlement.transaction_id == transaction.id
        and entitlement.provider_payment_id == payment_attempt.provider_payment_id
        and entitlement.quote_id == transaction.quote_id == quote.id
        and entitlement.quote_hash == transaction.quote_hash == quote.quote_hash
        and entitlement.merchant_id == transaction.merchant_id == quote.merchant_id
        and entitlement.service_id == transaction.service_id == quote.service_id
        and entitlement.amount == transaction.amount == quote.amount
        and entitlement.currency == transaction.currency == quote.currency
        and fulfillment.entitlement_id == entitlement.id
        and fulfillment.account_id == transaction.account_id
        and fulfillment.transaction_id == transaction.id
        and fulfillment.merchant_id == transaction.merchant_id
        and fulfillment.service_id == transaction.service_id
        and fulfillment.input_hash == entitlement.input_hash == quote.input_hash
    )


def _not_eligible(reason_code: CompensationPolicyReasonCode) -> CompensationPolicyResult:
    return CompensationPolicyResult(
        create_case=False,
        recommended_action=CompensationRecommendedAction.NO_REFUND,
        decision_state=None,
        decision_provenance=None,
        approved_refund_amount=None,
        reason_code=reason_code,
    )


def _manual_review(reason_code: CompensationPolicyReasonCode) -> CompensationPolicyResult:
    return CompensationPolicyResult(
        create_case=True,
        recommended_action=CompensationRecommendedAction.MANUAL_REVIEW,
        decision_state=CompensationDecisionState.MANUAL_REVIEW,
        decision_provenance=CompensationDecisionProvenance.MANUAL_REVIEW,
        approved_refund_amount=None,
        reason_code=reason_code,
    )
