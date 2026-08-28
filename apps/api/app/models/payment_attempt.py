"""Normalized Razorpay payment-attempt evidence."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
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

from app.domain.enums import PaymentAttemptStatus, PaymentProvider
from app.domain.ids import new_payment_attempt_id
from app.domain.payment_state import can_transition_attempt_status
from app.models.base import Base


class PaymentAttempt(Base):
    """One provider payment attached to a MeterGate transaction/order."""

    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint(
            "provider_payment_id",
            name="uq_payment_attempts_provider_payment_id",
        ),
        ForeignKeyConstraint(
            ["transaction_id", "provider_order_id"],
            ["payment_transactions.id", "payment_transactions.provider_order_id"],
            name="fk_payment_attempts_transaction_order",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'pmt_'",
            name="id_length_prefix",
        ),
        CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name="transaction_id_length_prefix",
        ),
        CheckConstraint("provider = 'razorpay'", name="provider"),
        CheckConstraint(
            "length(provider_order_id) BETWEEN 1 AND 64 "
            "AND provider_order_id = trim(provider_order_id)",
            name="provider_order_id_valid",
        ),
        CheckConstraint(
            "length(provider_payment_id) BETWEEN 1 AND 64 "
            "AND provider_payment_id = trim(provider_payment_id)",
            name="provider_payment_id_valid",
        ),
        CheckConstraint(
            "amount BETWEEN 1 AND 9007199254740991",
            name="amount_safe_integer_range",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="currency_uppercase",
        ),
        CheckConstraint(
            "provider_status IN ('created', 'authorized', 'captured', 'failed', 'refunded')",
            name="provider_status",
        ),
        CheckConstraint(
            "method IS NULL OR (length(method) BETWEEN 1 AND 32 AND method = trim(method))",
            name="method_valid",
        ),
        CheckConstraint(
            "(provider_status = 'captured' AND captured) OR provider_status <> 'captured'",
            name="captured_status_sets_flag",
        ),
        CheckConstraint(
            "NOT captured OR provider_status IN ('captured', 'refunded')",
            name="captured_flag_matches_status",
        ),
        CheckConstraint("last_seen_at >= first_seen_at", name="last_seen_valid"),
        CheckConstraint("updated_at >= created_at", name="updated_at_valid"),
        CheckConstraint(
            "id ~ '^pmt_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "transaction_id ~ '^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="transaction_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "provider_order_id ~ '^order_[A-Za-z0-9]+$'",
            name="provider_order_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "provider_payment_id ~ '^pay_[A-Za-z0-9]+$'",
            name="provider_payment_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="currency_format",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_payment_attempts_transaction_first_seen_at",
            "transaction_id",
            "first_seen_at",
        ),
        Index(
            "uq_payment_attempts_one_captured_per_transaction",
            "transaction_id",
            unique=True,
            postgresql_where=text("captured"),
            sqlite_where=text("captured = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
        default=new_payment_attempt_id,
    )
    transaction_id: Mapped[str] = mapped_column(String(30), nullable=False)
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
    provider_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_payment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    provider_status: Mapped[PaymentAttemptStatus] = mapped_column(
        Enum(
            PaymentAttemptStatus,
            name="payment_attempt_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    captured: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    provider_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


@event.listens_for(PaymentAttempt, "before_update")
def _guard_payment_attempt_update(
    _mapper: object,
    _connection: object,
    attempt: PaymentAttempt,
) -> None:
    state = sqlalchemy_inspect(attempt)
    immutable_fields = (
        "id",
        "transaction_id",
        "provider",
        "provider_order_id",
        "provider_payment_id",
        "amount",
        "currency",
        "method",
        "provider_created_at",
        "first_seen_at",
        "created_at",
    )
    if any(state.attrs[field].history.has_changes() for field in immutable_fields):
        raise InvalidRequestError("Payment attempt binding fields are immutable")

    status_history = state.attrs.provider_status.history
    if status_history.has_changes() and status_history.deleted:
        if not can_transition_attempt_status(
            status_history.deleted[0],
            attempt.provider_status,
        ):
            raise InvalidRequestError("Payment attempt status transition is not allowed")

    captured_history = state.attrs.captured.history
    if captured_history.has_changes() and captured_history.deleted:
        if captured_history.deleted[0] and not attempt.captured:
            raise InvalidRequestError("Payment attempt captured evidence cannot regress")

    for field in ("last_seen_at", "updated_at"):
        history = state.attrs[field].history
        if history.has_changes() and history.deleted:
            prior_value = history.deleted[0]
            if getattr(attempt, field) < prior_value:
                raise InvalidRequestError(f"Payment attempt {field} cannot regress")


@event.listens_for(PaymentAttempt, "before_delete")
def _reject_payment_attempt_delete(*_: object) -> None:
    raise InvalidRequestError("Payment attempts cannot be deleted")
