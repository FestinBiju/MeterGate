"""Auditable monetary-remediation decision for one terminal fulfillment failure."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    CompensationDecisionProvenance,
    CompensationDecisionState,
    CompensationRecommendedAction,
)
from app.domain.ids import new_compensation_case_id
from app.models.base import Base

_CASE_TRANSITIONS = {
    CompensationDecisionState.PENDING: {
        CompensationDecisionState.APPROVED,
        CompensationDecisionState.REJECTED,
        CompensationDecisionState.MANUAL_REVIEW,
    },
    CompensationDecisionState.MANUAL_REVIEW: {
        CompensationDecisionState.APPROVED,
        CompensationDecisionState.REJECTED,
    },
    CompensationDecisionState.APPROVED: {CompensationDecisionState.EXECUTING},
    CompensationDecisionState.EXECUTING: {
        CompensationDecisionState.COMPLETED,
        CompensationDecisionState.MANUAL_REVIEW,
    },
    CompensationDecisionState.REJECTED: set(),
    CompensationDecisionState.COMPLETED: set(),
}


class CompensationCase(Base):
    """One deterministic or operator-reviewed response to undelivered paid value."""

    __tablename__ = "compensation_cases"
    __table_args__ = (
        UniqueConstraint(
            "fulfillment_execution_id",
            name="uq_compensation_cases_fulfillment_execution_id",
        ),
        CheckConstraint("length(id) = 30 AND substr(id, 1, 4) = 'cmp_'", name="id_prefix"),
        CheckConstraint(
            "length(account_id) = 31 AND substr(account_id, 1, 5) = 'acct_'",
            name="account_id_prefix",
        ),
        CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name="transaction_id_prefix",
        ),
        CheckConstraint(
            "length(payment_attempt_id) = 30 AND substr(payment_attempt_id, 1, 4) = 'pmt_'",
            name="payment_attempt_id_prefix",
        ),
        CheckConstraint(
            "length(entitlement_id) = 30 AND substr(entitlement_id, 1, 4) = 'ent_'",
            name="entitlement_id_prefix",
        ),
        CheckConstraint(
            "length(fulfillment_execution_id) = 30 "
            "AND substr(fulfillment_execution_id, 1, 4) = 'ful_'",
            name="fulfillment_execution_id_prefix",
        ),
        CheckConstraint(
            "length(quote_id) = 30 AND substr(quote_id, 1, 4) = 'qte_'",
            name="quote_id_prefix",
        ),
        CheckConstraint(
            "length(merchant_id) = 30 AND substr(merchant_id, 1, 4) = 'mrc_'",
            name="merchant_id_prefix",
        ),
        CheckConstraint(
            "length(service_id) = 30 AND substr(service_id, 1, 4) = 'svc_'",
            name="service_id_prefix",
        ),
        CheckConstraint(
            "amount_paid BETWEEN 1 AND 9007199254740991",
            name="amount_paid_safe_integer",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="currency_uppercase",
        ),
        CheckConstraint(
            "length(failure_code) BETWEEN 1 AND 100 AND failure_code = trim(failure_code)",
            name="failure_code_valid",
        ),
        CheckConstraint("failure_evidence_version = '1'", name="failure_evidence_version"),
        CheckConstraint(
            "length(failure_evidence_hash) = 71 "
            "AND substr(failure_evidence_hash, 1, 7) = 'sha256:' "
            "AND failure_evidence_hash = lower(failure_evidence_hash)",
            name="failure_evidence_hash_shape",
        ),
        CheckConstraint(
            "length(quote_hash) = 71 AND substr(quote_hash, 1, 7) = 'sha256:' "
            "AND quote_hash = lower(quote_hash)",
            name="quote_hash_shape",
        ),
        CheckConstraint(
            "recommended_action IN ('full_refund', 'manual_review', 'no_refund')",
            name="recommended_action",
        ),
        CheckConstraint(
            "decision_state IN "
            "('pending', 'approved', 'rejected', 'executing', 'completed', 'manual_review')",
            name="decision_state",
        ),
        CheckConstraint(
            "decision_provenance IS NULL OR decision_provenance IN "
            "('automatic_approved', 'manual_approved', 'manual_rejected', 'manual_review')",
            name="decision_provenance",
        ),
        CheckConstraint(
            "approved_refund_amount IS NULL OR approved_refund_amount BETWEEN 1 AND amount_paid",
            name="approved_refund_amount_range",
        ),
        CheckConstraint(
            "decision_reason_code IS NULL OR "
            "(length(decision_reason_code) BETWEEN 1 AND 100 "
            "AND decision_reason_code = trim(decision_reason_code))",
            name="decision_reason_code_valid",
        ),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "decided_at IS NULL OR decided_at >= created_at",
            name="decided_after_creation",
        ),
        CheckConstraint(
            "closed_at IS NULL OR (decided_at IS NOT NULL AND closed_at >= decided_at)",
            name="closed_after_decision",
        ),
        CheckConstraint(
            "((decision_state = 'pending' AND decision_provenance IS NULL "
            "AND approved_refund_amount IS NULL AND decision_reason_code IS NULL "
            "AND decided_at IS NULL AND closed_at IS NULL) OR "
            "(decision_state IN ('approved', 'executing') "
            "AND decision_provenance IN ('automatic_approved', 'manual_approved') "
            "AND approved_refund_amount IS NOT NULL AND decision_reason_code IS NOT NULL "
            "AND decided_at IS NOT NULL AND closed_at IS NULL) OR "
            "(decision_state = 'completed' "
            "AND decision_provenance IN ('automatic_approved', 'manual_approved') "
            "AND approved_refund_amount IS NOT NULL AND decision_reason_code IS NOT NULL "
            "AND decided_at IS NOT NULL AND closed_at IS NOT NULL) OR "
            "(decision_state = 'manual_review' AND "
            "((decision_provenance = 'manual_review' AND approved_refund_amount IS NULL) OR "
            "(decision_provenance IN ('automatic_approved', 'manual_approved') "
            "AND approved_refund_amount IS NOT NULL)) "
            "AND decision_reason_code IS NOT NULL AND decided_at IS NOT NULL "
            "AND closed_at IS NULL) OR "
            "(decision_state = 'rejected' AND decision_provenance = 'manual_rejected' "
            "AND approved_refund_amount IS NULL AND decision_reason_code IS NOT NULL "
            "AND decided_at IS NOT NULL AND closed_at IS NOT NULL))",
            name="decision_state_payload",
        ),
        CheckConstraint(
            "decision_provenance <> 'automatic_approved' OR "
            "(recommended_action = 'full_refund' AND approved_refund_amount = amount_paid)",
            name="automatic_full_refund",
        ),
        CheckConstraint("id ~ '^cmp_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="id_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            "failure_code ~ '^[A-Z][A-Z0-9_]{0,99}$'", name="failure_code_format"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "failure_evidence_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="failure_evidence_hash_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("quote_hash ~ '^sha256:[0-9a-f]{64}$'", name="quote_hash_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            "decision_reason_code IS NULL OR decision_reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
            name="decision_reason_code_format",
        ).ddl_if(dialect="postgresql"),
        Index("ix_compensation_cases_account_created_at", "account_id", "created_at"),
        Index("ix_compensation_cases_transaction", "transaction_id"),
        Index("ix_compensation_cases_state_created_at", "decision_state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_compensation_case_id)
    account_id: Mapped[str] = mapped_column(
        String(31), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("payment_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    payment_attempt_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("payment_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    entitlement_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("entitlements.id", ondelete="RESTRICT"), nullable=False
    )
    fulfillment_execution_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("fulfillment_executions.id", ondelete="RESTRICT"), nullable=False
    )
    quote_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("quotes.id", ondelete="RESTRICT"), nullable=False
    )
    quote_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    merchant_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    amount_paid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    failure_code: Mapped[str] = mapped_column(String(100), nullable=False)
    failure_evidence_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="1", server_default="1"
    )
    failure_evidence_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    recommended_action: Mapped[CompensationRecommendedAction] = mapped_column(
        Enum(
            CompensationRecommendedAction,
            name="compensation_recommended_action",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    decision_state: Mapped[CompensationDecisionState] = mapped_column(
        Enum(
            CompensationDecisionState,
            name="compensation_decision_state",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=CompensationDecisionState.PENDING,
        server_default=CompensationDecisionState.PENDING.value,
        active_history=True,
    )
    decision_provenance: Mapped[CompensationDecisionProvenance | None] = mapped_column(
        Enum(
            CompensationDecisionProvenance,
            name="compensation_decision_provenance",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=True,
    )
    approved_refund_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decision_reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")


@event.listens_for(CompensationCase, "before_update")
def _guard_compensation_case_update(
    _mapper: object,
    _connection: object,
    case: CompensationCase,
) -> None:
    state = sqlalchemy_inspect(case)
    immutable = (
        "id",
        "account_id",
        "transaction_id",
        "payment_attempt_id",
        "entitlement_id",
        "fulfillment_execution_id",
        "quote_id",
        "quote_hash",
        "merchant_id",
        "service_id",
        "amount_paid",
        "currency",
        "failure_code",
        "failure_evidence_version",
        "failure_evidence_hash",
        "recommended_action",
        "created_at",
    )
    if any(state.attrs[field].history.has_changes() for field in immutable):
        raise InvalidRequestError("Compensation case evidence and bindings are immutable")

    revision_history = state.attrs.revision.history
    if not revision_history.has_changes() or not revision_history.deleted:
        raise InvalidRequestError("Compensation case revision must advance exactly once")
    if case.revision != revision_history.deleted[0] + 1:
        raise InvalidRequestError("Compensation case revision must advance exactly once")

    state_history = state.attrs.decision_state.history
    prior_state = CompensationDecisionState(
        state_history.deleted[0]
        if state_history.has_changes() and state_history.deleted
        else case.decision_state
    )
    resulting_state = CompensationDecisionState(case.decision_state)
    if prior_state in {
        CompensationDecisionState.REJECTED,
        CompensationDecisionState.COMPLETED,
    }:
        raise InvalidRequestError("Terminal compensation case evidence is immutable")
    if state_history.has_changes() and state_history.deleted:
        if resulting_state not in _CASE_TRANSITIONS[prior_state]:
            raise InvalidRequestError("Compensation case state transition is not allowed")

    if prior_state not in {
        CompensationDecisionState.PENDING,
        CompensationDecisionState.MANUAL_REVIEW,
    } and any(
        state.attrs[field].history.has_changes()
        for field in ("decision_provenance", "decision_reason_code")
    ):
        raise InvalidRequestError("Compensation decision provenance and reason are immutable")

    amount_history = state.attrs.approved_refund_amount.history
    if amount_history.has_changes() and amount_history.deleted:
        prior_amount = amount_history.deleted[0]
        if prior_amount is not None and case.approved_refund_amount != prior_amount:
            raise InvalidRequestError("Approved compensation amount is immutable once set")

    for field in ("decided_at", "closed_at"):
        history = state.attrs[field].history
        if not history.has_changes() or not history.deleted:
            continue
        prior_value = history.deleted[0]
        next_value = getattr(case, field)
        if prior_value is not None and next_value != prior_value:
            raise InvalidRequestError(f"Compensation case {field} is immutable once recorded")


@event.listens_for(CompensationCase, "before_delete")
def _reject_compensation_case_delete(*_: object) -> None:
    raise InvalidRequestError("Compensation cases cannot be deleted")
