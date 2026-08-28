"""Buyer-safe compensation and Razorpay Test Mode refund contracts."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StringConstraints

from app.schemas.common import APIModel, CurrencyCode

PaymentTransactionId = Annotated[
    str,
    StringConstraints(pattern=r"^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
ProviderPaymentId = Annotated[
    str,
    StringConstraints(pattern=r"^pay_[A-Za-z0-9]{1,60}$"),
]

CompensationId = Annotated[
    str,
    StringConstraints(pattern=r"^cmp_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
PaymentRefundId = Annotated[
    str,
    StringConstraints(pattern=r"^rfd_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
ProviderRefundId = Annotated[
    str,
    StringConstraints(pattern=r"^rfnd_[A-Za-z0-9]{1,59}$"),
]
PaymentAttemptId = Annotated[
    str,
    StringConstraints(pattern=r"^pmt_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
EntitlementId = Annotated[
    str,
    StringConstraints(pattern=r"^ent_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
FulfillmentExecutionId = Annotated[
    str,
    StringConstraints(pattern=r"^ful_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
CompensationAmount = Annotated[StrictInt, Field(ge=1, le=(1 << 53) - 1)]
CompensationDecisionStateValue = Literal[
    "pending",
    "approved",
    "rejected",
    "executing",
    "completed",
    "manual_review",
]
CompensationRecommendedActionValue = Literal["full_refund", "manual_review", "no_refund"]
CompensationDecisionProvenanceValue = Literal[
    "automatic_approved",
    "manual_approved",
    "manual_rejected",
    "manual_review",
]
PaymentRefundStateValue = Literal[
    "refund_pending",
    "refund_processing",
    "refunded",
    "refund_failed",
    "refund_uncertain",
    "reconciliation_required",
]
RefundProviderStatusValue = Literal["pending", "processed", "failed"]
CommerceOutcome = Literal[
    "payment_pending",
    "paid",
    "fulfillment_pending",
    "fulfilled",
    "compensation_pending",
    "manual_review",
    "refunded",
]


class CompensationSummary(APIModel):
    compensation_id: CompensationId
    fulfillment_execution_id: FulfillmentExecutionId
    failure_code: str = Field(min_length=1, max_length=100)
    recommended_action: CompensationRecommendedActionValue
    decision_state: CompensationDecisionStateValue
    decision_provenance: CompensationDecisionProvenanceValue | None
    approved_refund_amount: CompensationAmount | None
    decision_reason_code: str | None = Field(default=None, min_length=1, max_length=100)


class RefundSummary(APIModel):
    refund_id: PaymentRefundId
    provider_payment_id: ProviderPaymentId
    provider_refund_id: ProviderRefundId | None
    amount: CompensationAmount
    currency: CurrencyCode
    state: PaymentRefundStateValue
    provider_status: RefundProviderStatusValue | None
    reconciliation_required_at: datetime | None = None
    reconciliation_reason_code: str | None = Field(default=None, min_length=1, max_length=100)


class CompensationCaseResponse(APIModel):
    reason_code: str = Field(min_length=1, max_length=100)
    compensation_id: CompensationId
    transaction_id: PaymentTransactionId
    payment_attempt_id: PaymentAttemptId
    provider_payment_id: ProviderPaymentId
    entitlement_id: EntitlementId
    fulfillment_execution_id: FulfillmentExecutionId
    merchant_id: str
    service_id: str
    amount_paid: CompensationAmount
    currency: CurrencyCode
    failure_code: str = Field(min_length=1, max_length=100)
    failure_evidence_hash: Annotated[
        str,
        StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
    ]
    recommended_action: CompensationRecommendedActionValue
    decision_state: CompensationDecisionStateValue
    decision_provenance: CompensationDecisionProvenanceValue | None
    approved_refund_amount: CompensationAmount | None
    decision_reason_code: str | None = Field(default=None, min_length=1, max_length=100)
    created_at: datetime
    decided_at: datetime | None
    closed_at: datetime | None
    refund: RefundSummary | None


class PaymentRefundResponse(APIModel):
    reason_code: str = Field(min_length=1, max_length=100)
    refund_id: PaymentRefundId
    compensation_id: CompensationId
    transaction_id: PaymentTransactionId
    payment_attempt_id: PaymentAttemptId
    provider: Literal["razorpay"] = "razorpay"
    provider_payment_id: ProviderPaymentId
    provider_refund_id: ProviderRefundId | None
    provider_receipt: PaymentRefundId
    amount: CompensationAmount
    currency: CurrencyCode
    state: PaymentRefundStateValue
    provider_status: RefundProviderStatusValue | None
    created_at: datetime
    requested_at: datetime | None
    processed_at: datetime | None
    failed_at: datetime | None
    last_reconciled_at: datetime | None
    reconciliation_required_at: datetime | None = None
    reconciliation_reason_code: str | None = Field(default=None, min_length=1, max_length=100)
    revision: StrictInt = Field(ge=1)
