"""Durable lease-fenced queue for one compensation refund request."""

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

from app.domain.enums import RefundOutboxEventType
from app.domain.ids import new_refund_outbox_event_id
from app.models.base import Base, JSONObject


class RefundOutboxEvent(Base):
    """Retryable provider work committed atomically with compensation approval."""

    __tablename__ = "refund_outbox_events"
    __table_args__ = (
        UniqueConstraint("compensation_case_id", name="uq_refund_outbox_events_case_id"),
        UniqueConstraint("deduplication_key", name="uq_refund_outbox_events_dedup_key"),
        CheckConstraint("length(id) = 30 AND substr(id, 1, 4) = 'rox_'", name="id_prefix"),
        CheckConstraint("event_type = 'refund_requested'", name="event_type"),
        CheckConstraint(
            "length(compensation_case_id) = 30 AND substr(compensation_case_id, 1, 4) = 'cmp_'",
            name="compensation_case_id_prefix",
        ),
        CheckConstraint(
            "deduplication_key = 'refund:' || compensation_case_id",
            name="deduplication_binding",
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
        CheckConstraint("id ~ '^rox_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="id_format").ddl_if(
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
            "ix_refund_outbox_events_due",
            "available_at",
            "created_at",
            postgresql_where=text("processed_at IS NULL"),
            sqlite_where=text("processed_at IS NULL"),
        ),
        Index(
            "ix_refund_outbox_events_stale_lease",
            "lease_expires_at",
            postgresql_where=text("processed_at IS NULL AND lease_expires_at IS NOT NULL"),
            sqlite_where=text("processed_at IS NULL AND lease_expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(30), primary_key=True, default=new_refund_outbox_event_id
    )
    event_type: Mapped[RefundOutboxEventType] = mapped_column(
        Enum(
            RefundOutboxEventType,
            name="refund_outbox_event_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=RefundOutboxEventType.REFUND_REQUESTED,
        server_default=RefundOutboxEventType.REFUND_REQUESTED.value,
    )
    compensation_case_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("compensation_cases.id", ondelete="RESTRICT"), nullable=False
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


@event.listens_for(RefundOutboxEvent, "before_update")
def _guard_refund_outbox_update(
    _mapper: object,
    _connection: object,
    record: RefundOutboxEvent,
) -> None:
    state = sqlalchemy_inspect(record)
    immutable = (
        "id",
        "event_type",
        "compensation_case_id",
        "deduplication_key",
        "payload_version",
        "payload",
        "created_at",
    )
    if any(state.attrs[field].history.has_changes() for field in immutable):
        raise InvalidRequestError("Refund outbox binding and payload are immutable")
    for field in ("attempt_count", "lease_generation"):
        history = state.attrs[field].history
        if (
            history.has_changes()
            and history.deleted
            and getattr(record, field) < history.deleted[0]
        ):
            raise InvalidRequestError(f"Refund outbox {field} cannot regress")
    processed_history = state.attrs.processed_at.history
    if processed_history.has_changes() and processed_history.deleted:
        prior = processed_history.deleted[0]
        if prior is not None and record.processed_at != prior:
            raise InvalidRequestError("Processed refund outbox work is terminal")


@event.listens_for(RefundOutboxEvent, "before_delete")
def _reject_refund_outbox_delete(*_: object) -> None:
    raise InvalidRequestError("Refund outbox events cannot be deleted")
