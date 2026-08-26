"""Append-only compensation and refund audit evidence."""

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

from app.domain.enums import CompensationEventActorType, CompensationEventType
from app.domain.ids import new_compensation_event_id
from app.models.base import Base, JSONObject


class CompensationEvent(Base):
    """One immutable, deterministically ordered observation in compensation."""

    __tablename__ = "compensation_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_compensation_events_idempotency_key"),
        UniqueConstraint(
            "compensation_case_id",
            "sequence",
            name="uq_compensation_events_case_sequence",
        ),
        CheckConstraint("length(id) = 30 AND substr(id, 1, 4) = 'cpe_'", name="id_prefix"),
        CheckConstraint(
            "length(compensation_case_id) = 30 AND substr(compensation_case_id, 1, 4) = 'cmp_'",
            name="compensation_case_id_prefix",
        ),
        CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name="transaction_id_prefix",
        ),
        CheckConstraint(
            "payment_refund_id IS NULL OR "
            "(length(payment_refund_id) = 30 AND substr(payment_refund_id, 1, 4) = 'rfd_')",
            name="payment_refund_id_prefix",
        ),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("case_revision >= 1", name="case_revision_positive"),
        CheckConstraint(
            "refund_revision IS NULL OR refund_revision >= 1", name="refund_revision_positive"
        ),
        CheckConstraint(
            "event_type IN ('compensation_case_created', 'compensation_recommended', "
            "'compensation_approved', 'compensation_rejected', 'refund_outbox_created', "
            "'refund_requested', 'refund_request_started', 'razorpay_refund_created', "
            "'refund_processing', 'refund_completed', 'refund_failed', 'refund_uncertain', "
            "'refund_reconciliation_required', 'refund_reconciliation_resolved', "
            "'compensation_closed')",
            name="event_type",
        ),
        CheckConstraint(
            "actor_type IN "
            "('system', 'refund_worker', 'provider_api', 'provider_webhook', 'operator')",
            name="actor_type",
        ),
        CheckConstraint(
            "actor_id IS NULL OR "
            "(length(actor_id) BETWEEN 1 AND 128 AND actor_id = trim(actor_id))",
            name="actor_id_valid",
        ),
        CheckConstraint(
            "((event_type IN ('compensation_case_created', 'compensation_recommended', "
            "'compensation_approved', 'compensation_rejected', 'refund_outbox_created') "
            "AND payment_refund_id IS NULL AND refund_revision IS NULL) OR "
            "(event_type IN ('refund_requested', 'refund_request_started', "
            "'razorpay_refund_created', 'refund_processing', 'refund_completed', "
            "'refund_failed', 'refund_uncertain', 'refund_reconciliation_required', "
            "'refund_reconciliation_resolved') "
            "AND payment_refund_id IS NOT NULL AND refund_revision IS NOT NULL) OR "
            "(event_type = 'compensation_closed' AND "
            "((payment_refund_id IS NULL AND refund_revision IS NULL) OR "
            "(payment_refund_id IS NOT NULL AND refund_revision IS NOT NULL))))",
            name="event_binding",
        ),
        CheckConstraint(
            "length(reason_code) BETWEEN 1 AND 100 AND reason_code = trim(reason_code)",
            name="reason_code_valid",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 AND idempotency_key = trim(idempotency_key)",
            name="idempotency_key_valid",
        ),
        CheckConstraint("id ~ '^cpe_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="id_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint("reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'", name="reason_code_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object").ddl_if(
            dialect="postgresql"
        ),
        Index(
            "ix_compensation_events_transaction_occurred",
            "transaction_id",
            "occurred_at",
        ),
        Index(
            "ix_compensation_events_case_sequence",
            "compensation_case_id",
            "sequence",
        ),
        Index("ix_compensation_events_refund_occurred", "payment_refund_id", "occurred_at"),
        Index("ix_compensation_events_type_occurred", "event_type", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_compensation_event_id)
    compensation_case_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("compensation_cases.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("payment_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    payment_refund_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("payment_refunds.id", ondelete="RESTRICT"), nullable=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    case_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    refund_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_type: Mapped[CompensationEventType] = mapped_column(
        Enum(
            CompensationEventType,
            name="compensation_event_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    actor_type: Mapped[CompensationEventActorType] = mapped_column(
        Enum(
            CompensationEventActorType,
            name="compensation_event_actor_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONObject(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@event.listens_for(CompensationEvent, "before_update")
@event.listens_for(CompensationEvent, "before_delete")
def _reject_compensation_event_mutation(*_: object) -> None:
    raise InvalidRequestError("Compensation events are immutable")
