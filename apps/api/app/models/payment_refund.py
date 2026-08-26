"""Durable, monotonic Razorpay refund state separate from payment history."""

from datetime import UTC, datetime

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
    text,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import PaymentProvider, PaymentRefundState
from app.domain.ids import new_payment_refund_id
from app.models.base import Base

_REFUND_TRANSITIONS = {
    PaymentRefundState.REFUND_PENDING: {
        PaymentRefundState.REFUND_PROCESSING,
        PaymentRefundState.REFUND_FAILED,
        PaymentRefundState.REFUND_UNCERTAIN,
        PaymentRefundState.RECONCILIATION_REQUIRED,
    },
    PaymentRefundState.REFUND_PROCESSING: {
        PaymentRefundState.REFUNDED,
        PaymentRefundState.REFUND_FAILED,
        PaymentRefundState.REFUND_UNCERTAIN,
        PaymentRefundState.RECONCILIATION_REQUIRED,
    },
    PaymentRefundState.REFUND_UNCERTAIN: {
        PaymentRefundState.REFUND_PROCESSING,
        PaymentRefundState.REFUNDED,
        PaymentRefundState.REFUND_FAILED,
        PaymentRefundState.RECONCILIATION_REQUIRED,
    },
    PaymentRefundState.RECONCILIATION_REQUIRED: {
        PaymentRefundState.REFUND_PROCESSING,
        PaymentRefundState.REFUNDED,
        PaymentRefundState.REFUND_FAILED,
    },
    PaymentRefundState.REFUNDED: set(),
    PaymentRefundState.REFUND_FAILED: set(),
}


def _utc_for_guard(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive round trips for monotonic comparisons."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class PaymentRefund(Base):
    """One reserved partial or full refund against a compensation decision."""

    __tablename__ = "payment_refunds"
    __table_args__ = (
        UniqueConstraint("provider_refund_id", name="uq_payment_refunds_provider_refund_id"),
        UniqueConstraint("provider_receipt", name="uq_payment_refunds_provider_receipt"),
        CheckConstraint("length(id) = 30 AND substr(id, 1, 4) = 'rfd_'", name="id_prefix"),
        CheckConstraint(
            "length(compensation_case_id) = 30 AND substr(compensation_case_id, 1, 4) = 'cmp_'",
            name="compensation_case_id_prefix",
        ),
        CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name="transaction_id_prefix",
        ),
        CheckConstraint(
            "length(payment_attempt_id) = 30 AND substr(payment_attempt_id, 1, 4) = 'pmt_'",
            name="payment_attempt_id_prefix",
        ),
        CheckConstraint("provider = 'razorpay'", name="provider"),
        CheckConstraint(
            "length(provider_payment_id) BETWEEN 1 AND 64 "
            "AND provider_payment_id = trim(provider_payment_id)",
            name="provider_payment_id_valid",
        ),
        CheckConstraint(
            "provider_refund_id IS NULL OR "
            "(length(provider_refund_id) BETWEEN 1 AND 64 "
            "AND provider_refund_id = trim(provider_refund_id))",
            name="provider_refund_id_valid",
        ),
        CheckConstraint("amount BETWEEN 1 AND 9007199254740991", name="amount_safe_integer"),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)", name="currency_uppercase"
        ),
        CheckConstraint(
            "refund_state IN ('refund_pending', 'refund_processing', 'refunded', "
            "'refund_failed', 'refund_uncertain', 'reconciliation_required')",
            name="refund_state",
        ),
        CheckConstraint(
            "provider_status IS NULL OR provider_status IN ('pending', 'processed', 'failed')",
            name="provider_status",
        ),
        CheckConstraint(
            "(refund_state <> 'refunded' OR provider_status = 'processed') AND "
            "(refund_state <> 'refund_failed' OR "
            "((provider_refund_id IS NULL AND provider_status IS NULL) OR "
            "(provider_refund_id IS NOT NULL AND provider_status = 'failed')))",
            name="terminal_provider_evidence",
        ),
        CheckConstraint(
            "length(provider_receipt) BETWEEN 1 AND 40 "
            "AND provider_receipt = trim(provider_receipt)",
            name="provider_receipt_valid",
        ),
        CheckConstraint("provider_receipt = id", name="provider_receipt_is_id"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "requested_at IS NULL OR requested_at >= created_at", name="requested_after_creation"
        ),
        CheckConstraint(
            "processed_at IS NULL OR (requested_at IS NOT NULL AND processed_at >= requested_at)",
            name="processed_after_request",
        ),
        CheckConstraint(
            "failed_at IS NULL OR (requested_at IS NOT NULL AND failed_at >= requested_at)",
            name="failed_after_request",
        ),
        CheckConstraint(
            "last_reconciled_at IS NULL OR "
            "(requested_at IS NOT NULL AND last_reconciled_at >= requested_at)",
            name="reconciled_after_request",
        ),
        CheckConstraint(
            "((reconciliation_required_at IS NULL AND reconciliation_reason_code IS NULL) OR "
            "(reconciliation_required_at IS NOT NULL "
            "AND reconciliation_reason_code IS NOT NULL))",
            name="reconciliation_overlay_pair",
        ),
        CheckConstraint(
            "reconciliation_required_at IS NULL OR "
            "(requested_at IS NOT NULL AND reconciliation_required_at >= requested_at)",
            name="reconciliation_required_after_request",
        ),
        CheckConstraint(
            "reconciliation_reason_code IS NULL OR "
            "(length(reconciliation_reason_code) BETWEEN 1 AND 100 "
            "AND reconciliation_reason_code = trim(reconciliation_reason_code) "
            "AND reconciliation_reason_code = upper(reconciliation_reason_code))",
            name="reconciliation_reason_code_valid",
        ),
        CheckConstraint(
            "((refund_state = 'refund_pending' AND provider_refund_id IS NULL "
            "AND provider_status IS NULL AND requested_at IS NULL "
            "AND processed_at IS NULL AND failed_at IS NULL) OR "
            "(refund_state IN "
            "('refund_processing', 'refund_uncertain', 'reconciliation_required') "
            "AND requested_at IS NOT NULL AND processed_at IS NULL AND failed_at IS NULL) OR "
            "(refund_state = 'refunded' AND provider_refund_id IS NOT NULL "
            "AND provider_status IS NOT NULL AND requested_at IS NOT NULL "
            "AND processed_at IS NOT NULL AND failed_at IS NULL) OR "
            "(refund_state = 'refund_failed' AND requested_at IS NOT NULL "
            "AND processed_at IS NULL AND failed_at IS NOT NULL))",
            name="state_payload",
        ),
        CheckConstraint("id ~ '^rfd_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="id_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            "provider_payment_id ~ '^pay_[A-Za-z0-9]+$'", name="provider_payment_id_format"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            "reconciliation_reason_code IS NULL OR "
            "reconciliation_reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
            name="reconciliation_reason_code_format",
        ).ddl_if(dialect="postgresql"),
        Index("ix_payment_refunds_transaction_created_at", "transaction_id", "created_at"),
        Index("ix_payment_refunds_case_created_at", "compensation_case_id", "created_at"),
        Index("ix_payment_refunds_state_created_at", "refund_state", "created_at"),
        Index(
            "uq_payment_refunds_one_active_per_case",
            "compensation_case_id",
            unique=True,
            postgresql_where=text(
                "refund_state IN ('refund_pending', 'refund_processing', "
                "'refund_uncertain', 'reconciliation_required')"
            ),
            sqlite_where=text(
                "refund_state IN ('refund_pending', 'refund_processing', "
                "'refund_uncertain', 'reconciliation_required')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_payment_refund_id)
    compensation_case_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("compensation_cases.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("payment_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    payment_attempt_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("payment_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        Enum(
            PaymentProvider,
            name="payment_provider",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=PaymentProvider.RAZORPAY,
        server_default=PaymentProvider.RAZORPAY.value,
    )
    provider_payment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("payment_attempts.provider_payment_id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_refund_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    refund_state: Mapped[PaymentRefundState] = mapped_column(
        Enum(
            PaymentRefundState,
            name="payment_refund_state",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=PaymentRefundState.REFUND_PENDING,
        server_default=PaymentRefundState.REFUND_PENDING.value,
        active_history=True,
    )
    provider_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_receipt: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconciliation_required_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconciliation_reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")


@event.listens_for(PaymentRefund, "before_insert")
def _validate_payment_refund_insert(
    _mapper: object,
    _connection: object,
    refund: PaymentRefund,
) -> None:
    if refund.provider_receipt != refund.id:
        raise InvalidRequestError("Refund provider receipt must equal the refund ID")


@event.listens_for(PaymentRefund, "before_update")
def _guard_payment_refund_update(
    _mapper: object,
    _connection: object,
    refund: PaymentRefund,
) -> None:
    state = sqlalchemy_inspect(refund)
    immutable = (
        "id",
        "compensation_case_id",
        "transaction_id",
        "payment_attempt_id",
        "provider",
        "provider_payment_id",
        "amount",
        "currency",
        "provider_receipt",
        "created_at",
    )
    if any(state.attrs[field].history.has_changes() for field in immutable):
        raise InvalidRequestError("Payment refund binding and amount are immutable")

    revision_history = state.attrs.revision.history
    if not revision_history.has_changes() or not revision_history.deleted:
        raise InvalidRequestError("Payment refund revision must advance exactly once")
    if refund.revision != revision_history.deleted[0] + 1:
        raise InvalidRequestError("Payment refund revision must advance exactly once")

    state_history = state.attrs.refund_state.history
    prior_state = PaymentRefundState(
        state_history.deleted[0]
        if state_history.has_changes() and state_history.deleted
        else refund.refund_state
    )
    resulting_state = PaymentRefundState(refund.refund_state)
    if prior_state in {PaymentRefundState.REFUNDED, PaymentRefundState.REFUND_FAILED}:
        terminal_evidence = (
            "id",
            "compensation_case_id",
            "transaction_id",
            "payment_attempt_id",
            "provider",
            "provider_payment_id",
            "provider_refund_id",
            "amount",
            "currency",
            "refund_state",
            "provider_status",
            "provider_receipt",
            "created_at",
            "requested_at",
            "processed_at",
            "failed_at",
        )
        if any(state.attrs[field].history.has_changes() for field in terminal_evidence):
            raise InvalidRequestError("Terminal payment refund evidence is immutable")
    if state_history.has_changes() and state_history.deleted:
        if resulting_state not in _REFUND_TRANSITIONS[prior_state]:
            raise InvalidRequestError("Payment refund state transition is not allowed")

    provider_id_history = state.attrs.provider_refund_id.history
    if provider_id_history.has_changes() and provider_id_history.deleted:
        prior_provider_id = provider_id_history.deleted[0]
        if prior_provider_id is not None and refund.provider_refund_id != prior_provider_id:
            raise InvalidRequestError("Provider refund binding is immutable once set")

    for field in ("requested_at", "processed_at", "failed_at"):
        history = state.attrs[field].history
        if not history.has_changes() or not history.deleted:
            continue
        prior_value = history.deleted[0]
        next_value = getattr(refund, field)
        if prior_value is not None and next_value != prior_value:
            raise InvalidRequestError(f"Payment refund {field} is immutable once recorded")

    reconciled_history = state.attrs.last_reconciled_at.history
    if reconciled_history.has_changes() and reconciled_history.deleted:
        prior_reconciled_at = reconciled_history.deleted[0]
        if prior_reconciled_at is not None and (
            refund.last_reconciled_at is None
            or _utc_for_guard(refund.last_reconciled_at) < _utc_for_guard(prior_reconciled_at)
        ):
            raise InvalidRequestError("Payment refund reconciliation time cannot regress")

    overlay_history = state.attrs.reconciliation_required_at.history
    if overlay_history.has_changes() and overlay_history.deleted:
        prior_required_at = overlay_history.deleted[0]
        if (
            prior_required_at is not None
            and refund.reconciliation_required_at is not None
            and _utc_for_guard(refund.reconciliation_required_at)
            < _utc_for_guard(prior_required_at)
        ):
            raise InvalidRequestError("Refund reconciliation overlay time cannot regress")


@event.listens_for(PaymentRefund, "before_delete")
def _reject_payment_refund_delete(*_: object) -> None:
    raise InvalidRequestError("Payment refunds cannot be deleted")
