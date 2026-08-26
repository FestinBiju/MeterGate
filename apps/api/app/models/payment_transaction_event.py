"""Append-only audit events for payment transaction state revisions."""

from datetime import datetime
from typing import Any

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
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    PaymentEventActorType,
    PaymentTransactionEventType,
    PaymentTransactionState,
)
from app.domain.ids import new_payment_transaction_event_id
from app.models.base import Base, JSONObject


class PaymentTransactionEvent(Base):
    """Durable evidence for one aggregate revision or meaningful observation."""

    __tablename__ = "payment_transaction_events"
    __table_args__ = (
        UniqueConstraint(
            "transaction_id",
            "transaction_revision",
            name="uq_payment_transaction_events_transaction_revision",
        ),
        UniqueConstraint(
            "source_webhook_event_id",
            name="uq_payment_transaction_events_source_webhook_event_id",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_payment_transaction_events_idempotency_key",
        ),
        CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'pte_'",
            name="id_length_prefix",
        ),
        CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name="transaction_id_length_prefix",
        ),
        CheckConstraint("transaction_revision >= 1", name="transaction_revision_positive"),
        CheckConstraint(
            "event_type IN ("
            "'payment_transaction_created', 'razorpay_order_creation_started', "
            "'razorpay_order_created', 'razorpay_order_creation_failed', "
            "'razorpay_order_creation_uncertain', 'checkout_signature_verified', "
            "'payment_authorized', 'payment_attempt_failed', 'payment_captured', "
            "'order_paid', 'payment_reconciled', 'reconciliation_required')",
            name="event_type",
        ),
        CheckConstraint(
            "actor_type IN ('account', 'system', 'provider_api', 'provider_webhook')",
            name="actor_type",
        ),
        CheckConstraint(
            "actor_id IS NULL OR "
            "(length(actor_id) BETWEEN 1 AND 128 AND actor_id = trim(actor_id))",
            name="actor_id_valid",
        ),
        CheckConstraint(
            "prior_state IS NULL OR prior_state IN ("
            "'order_creation_pending', 'order_created', 'order_creation_failed', "
            "'order_creation_uncertain', 'payment_pending', 'payment_authorized', "
            "'paid', 'reconciliation_required')",
            name="prior_state",
        ),
        CheckConstraint(
            "resulting_state IN ("
            "'order_creation_pending', 'order_created', 'order_creation_failed', "
            "'order_creation_uncertain', 'payment_pending', 'payment_authorized', "
            "'paid', 'reconciliation_required')",
            name="resulting_state",
        ),
        CheckConstraint(
            "(transaction_revision = 1 AND prior_state IS NULL) "
            "OR (transaction_revision > 1 AND prior_state IS NOT NULL)",
            name="prior_state_matches_revision",
        ),
        CheckConstraint(
            "length(reason_code) BETWEEN 1 AND 100 AND reason_code = trim(reason_code)",
            name="reason_code_valid",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 AND idempotency_key = trim(idempotency_key)",
            name="idempotency_key_valid",
        ),
        CheckConstraint(
            "payment_attempt_id IS NULL OR "
            "(length(payment_attempt_id) = 30 "
            "AND substr(payment_attempt_id, 1, 4) = 'pmt_')",
            name="payment_attempt_id_length_prefix",
        ),
        CheckConstraint(
            "source_webhook_event_id IS NULL OR "
            "(length(source_webhook_event_id) = 30 "
            "AND substr(source_webhook_event_id, 1, 4) = 'rwe_')",
            name="source_webhook_id_prefix",
        ),
        CheckConstraint(
            "id ~ '^pte_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "transaction_id ~ '^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="transaction_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
            name="reason_code_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "payment_attempt_id IS NULL "
            "OR payment_attempt_id ~ '^pmt_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="payment_attempt_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "source_webhook_event_id IS NULL "
            "OR source_webhook_event_id ~ '^rwe_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="source_webhook_event_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(metadata) = 'object'",
            name="metadata_object",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_payment_transaction_events_transaction_occurred_at",
            "transaction_id",
            "occurred_at",
        ),
        Index(
            "ix_payment_transaction_events_type_occurred_at",
            "event_type",
            "occurred_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
        default=new_payment_transaction_event_id,
    )
    transaction_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("payment_transactions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    transaction_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[PaymentTransactionEventType] = mapped_column(
        Enum(
            PaymentTransactionEventType,
            name="payment_transaction_event_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    actor_type: Mapped[PaymentEventActorType] = mapped_column(
        Enum(
            PaymentEventActorType,
            name="payment_event_actor_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prior_state: Mapped[PaymentTransactionState | None] = mapped_column(
        Enum(
            PaymentTransactionState,
            name="payment_transaction_state",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=True,
    )
    resulting_state: Mapped[PaymentTransactionState] = mapped_column(
        Enum(
            PaymentTransactionState,
            name="payment_transaction_state",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONObject(),
        nullable=False,
    )
    payment_attempt_id: Mapped[str | None] = mapped_column(
        String(30),
        ForeignKey("payment_attempts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_webhook_event_id: Mapped[str | None] = mapped_column(
        String(30),
        ForeignKey("razorpay_webhook_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


@event.listens_for(PaymentTransactionEvent, "before_update")
@event.listens_for(PaymentTransactionEvent, "before_delete")
def _reject_payment_transaction_event_mutation(*_: object) -> None:
    raise InvalidRequestError("Payment transaction events are immutable")
