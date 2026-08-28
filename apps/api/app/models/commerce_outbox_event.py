"""Durable paid-transaction work queue for entitlement issuance."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import CommerceAggregateType, CommerceOutboxEventType
from app.domain.ids import new_commerce_outbox_event_id
from app.models.base import Base, JSONObject


class CommerceOutboxEvent(Base):
    """Retryable work committed atomically with a paid payment transition."""

    __tablename__ = "commerce_outbox_events"
    __table_args__ = (
        UniqueConstraint("deduplication_key", name="uq_commerce_outbox_events_dedup_key"),
        CheckConstraint("length(id) = 30 AND substr(id, 1, 4) = 'obx_'", name="id_prefix"),
        CheckConstraint("event_type = 'entitlement_issuance_requested'", name="event_type"),
        CheckConstraint("aggregate_type = 'payment_transaction'", name="aggregate_type"),
        CheckConstraint(
            "length(aggregate_id) = 30 AND substr(aggregate_id, 1, 4) = 'txn_'",
            name="aggregate_id_prefix",
        ),
        CheckConstraint(
            "deduplication_key = 'entitlement:' || aggregate_id", name="deduplication_binding"
        ),
        CheckConstraint("payload_version = '1'", name="payload_version"),
        CheckConstraint("available_at >= created_at", name="availability"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("lease_generation >= 0", name="lease_generation_nonnegative"),
        CheckConstraint(
            "(processing_started_at IS NULL AND lease_expires_at IS NULL) OR "
            "(processing_started_at IS NOT NULL AND lease_expires_at > processing_started_at)",
            name="lease_complete",
        ),
        CheckConstraint(
            "processed_at IS NULL OR processing_started_at IS NOT NULL", name="processed_claimed"
        ),
        CheckConstraint(
            "processed_at IS NULL OR processed_at >= processing_started_at",
            name="processed_after_claim",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR "
            "(length(last_error_code) BETWEEN 1 AND 100 "
            "AND last_error_code = trim(last_error_code))",
            name="last_error_code_valid",
        ),
        CheckConstraint("id ~ '^obx_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="id_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            "last_error_code IS NULL OR last_error_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
            name="last_error_code_format",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_commerce_outbox_events_due",
            "available_at",
            "created_at",
            postgresql_where=text("processed_at IS NULL"),
            sqlite_where=text("processed_at IS NULL"),
        ),
        Index(
            "ix_commerce_outbox_events_stale_lease",
            "lease_expires_at",
            postgresql_where=text("processed_at IS NULL AND lease_expires_at IS NOT NULL"),
            sqlite_where=text("processed_at IS NULL AND lease_expires_at IS NOT NULL"),
        ),
        Index("ix_commerce_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[str] = mapped_column(
        String(30), primary_key=True, default=new_commerce_outbox_event_id
    )
    event_type: Mapped[CommerceOutboxEventType] = mapped_column(
        Enum(
            CommerceOutboxEventType,
            name="commerce_outbox_event_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=CommerceOutboxEventType.ENTITLEMENT_ISSUANCE_REQUESTED,
        server_default=CommerceOutboxEventType.ENTITLEMENT_ISSUANCE_REQUESTED.value,
    )
    aggregate_type: Mapped[CommerceAggregateType] = mapped_column(
        Enum(
            CommerceAggregateType,
            name="commerce_aggregate_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=CommerceAggregateType.PAYMENT_TRANSACTION,
        server_default=CommerceAggregateType.PAYMENT_TRANSACTION.value,
    )
    aggregate_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("payment_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="1", server_default="1"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONObject(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    lease_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


@event.listens_for(CommerceOutboxEvent, "before_update")
def _guard_commerce_outbox_update(
    _mapper: object,
    _connection: object,
    record: CommerceOutboxEvent,
) -> None:
    state = sqlalchemy_inspect(record)
    immutable = (
        "id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "deduplication_key",
        "payload_version",
        "payload",
        "created_at",
    )
    if any(state.attrs[field].history.has_changes() for field in immutable):
        raise InvalidRequestError("Commerce outbox binding and payload are immutable")
    for field in ("attempt_count", "lease_generation"):
        history = state.attrs[field].history
        if (
            history.has_changes()
            and history.deleted
            and getattr(record, field) < history.deleted[0]
        ):
            raise InvalidRequestError(f"Commerce outbox {field} cannot regress")
    processed_history = state.attrs.processed_at.history
    if processed_history.has_changes() and processed_history.deleted:
        prior = processed_history.deleted[0]
        if prior is not None and record.processed_at != prior:
            raise InvalidRequestError("Processed commerce outbox work is terminal")


@event.listens_for(CommerceOutboxEvent, "before_delete")
def _reject_commerce_outbox_delete(*_: object) -> None:
    raise InvalidRequestError("Commerce outbox events cannot be deleted")
