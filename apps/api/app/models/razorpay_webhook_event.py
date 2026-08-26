"""Append-only normalized evidence for verified Razorpay webhooks."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, String, event, func
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import WebhookProcessingStatus
from app.domain.ids import new_razorpay_webhook_event_id
from app.models.base import Base


class RazorpayWebhookEvent(Base):
    """Final processing outcome for one unique provider event delivery."""

    __tablename__ = "razorpay_webhook_events"
    __table_args__ = (
        CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'rwe_'",
            name="id_length_prefix",
        ),
        CheckConstraint(
            "length(provider_event_id) BETWEEN 1 AND 128 "
            "AND provider_event_id = trim(provider_event_id)",
            name="provider_event_id_valid",
        ),
        CheckConstraint(
            "length(provider_event_type) BETWEEN 1 AND 100 "
            "AND provider_event_type = trim(provider_event_type)",
            name="provider_event_type_valid",
        ),
        CheckConstraint(
            "length(raw_body_hash) = 71 "
            "AND substr(raw_body_hash, 1, 7) = 'sha256:' "
            "AND raw_body_hash = lower(raw_body_hash)",
            name="raw_body_hash_shape",
        ),
        CheckConstraint(
            "processing_status IN ('processed', 'ignored', 'failed', 'reconciliation_required')",
            name="processing_status",
        ),
        CheckConstraint(
            "length(processing_reason_code) BETWEEN 1 AND 100 "
            "AND processing_reason_code = trim(processing_reason_code)",
            name="processing_reason_code_valid",
        ),
        CheckConstraint(
            "provider_order_id IS NULL OR "
            "(length(provider_order_id) BETWEEN 1 AND 64 "
            "AND provider_order_id = trim(provider_order_id))",
            name="provider_order_id_valid",
        ),
        CheckConstraint(
            "provider_payment_id IS NULL OR "
            "(length(provider_payment_id) BETWEEN 1 AND 64 "
            "AND provider_payment_id = trim(provider_payment_id))",
            name="provider_payment_id_valid",
        ),
        CheckConstraint(
            "transaction_id IS NULL OR "
            "(length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_')",
            name="transaction_id_length_prefix",
        ),
        CheckConstraint("processed_at >= received_at", name="processed_after_received"),
        CheckConstraint(
            "id ~ '^rwe_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "raw_body_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="raw_body_hash_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "processing_reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
            name="processing_reason_code_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "provider_order_id IS NULL OR provider_order_id ~ '^order_[A-Za-z0-9]+$'",
            name="provider_order_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "provider_payment_id IS NULL OR provider_payment_id ~ '^pay_[A-Za-z0-9]+$'",
            name="provider_payment_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "transaction_id IS NULL OR transaction_id ~ '^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="transaction_id_format",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_razorpay_webhook_events_transaction_received_at",
            "transaction_id",
            "received_at",
        ),
        Index("ix_razorpay_webhook_events_provider_order_id", "provider_order_id"),
        Index("ix_razorpay_webhook_events_provider_payment_id", "provider_payment_id"),
        Index(
            "ix_razorpay_webhook_events_status_received_at",
            "processing_status",
            "received_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
        default=new_razorpay_webhook_event_id,
    )
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    provider_event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_body_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    provider_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processing_status: Mapped[WebhookProcessingStatus] = mapped_column(
        Enum(
            WebhookProcessingStatus,
            name="webhook_processing_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    processing_reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transaction_id: Mapped[str | None] = mapped_column(
        String(30),
        ForeignKey("payment_transactions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


@event.listens_for(RazorpayWebhookEvent, "before_update")
@event.listens_for(RazorpayWebhookEvent, "before_delete")
def _reject_razorpay_webhook_event_mutation(*_: object) -> None:
    raise InvalidRequestError("Razorpay webhook evidence is immutable")
