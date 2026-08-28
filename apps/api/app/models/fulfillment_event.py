"""Append-only value-release and fulfillment audit evidence."""

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

from app.domain.enums import FulfillmentEventActorType, FulfillmentEventType
from app.domain.ids import new_fulfillment_event_id
from app.models.base import Base, JSONObject


class FulfillmentEvent(Base):
    """One immutable observation in the paid-value-release timeline."""

    __tablename__ = "fulfillment_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_fulfillment_events_idempotency_key"),
        CheckConstraint("length(id) = 30 AND substr(id, 1, 4) = 'fve_'", name="id_prefix"),
        CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name="transaction_id_prefix",
        ),
        CheckConstraint(
            "entitlement_id IS NULL OR "
            "(length(entitlement_id) = 30 AND substr(entitlement_id, 1, 4) = 'ent_')",
            name="entitlement_id_prefix",
        ),
        CheckConstraint(
            "execution_id IS NULL OR "
            "(length(execution_id) = 30 AND substr(execution_id, 1, 4) = 'ful_')",
            name="execution_id_prefix",
        ),
        CheckConstraint(
            "event_type IN ('payment_reverified', 'entitlement_issued', 'capability_issued', "
            "'fulfillment_claimed', 'fulfillment_started', 'merchant_request_sent', "
            "'merchant_response_received', 'fulfillment_succeeded', "
            "'fulfillment_retry_scheduled', 'fulfillment_failed', 'compensation_required')",
            name="event_type",
        ),
        CheckConstraint(
            "actor_type IN ('account', 'system', 'entitlement_worker', 'resource_gateway', "
            "'merchant_service')",
            name="actor_type",
        ),
        CheckConstraint(
            "actor_id IS NULL OR (length(actor_id) BETWEEN 1 AND 128 "
            "AND actor_id = trim(actor_id))",
            name="actor_id_valid",
        ),
        CheckConstraint(
            "((event_type = 'payment_reverified' AND entitlement_id IS NULL "
            "AND execution_id IS NULL AND execution_revision IS NULL) OR "
            "(event_type IN ('entitlement_issued', 'capability_issued') "
            "AND entitlement_id IS NOT NULL AND execution_id IS NULL "
            "AND execution_revision IS NULL) OR "
            "(event_type IN ('fulfillment_claimed', 'fulfillment_started', "
            "'merchant_request_sent', 'merchant_response_received', 'fulfillment_succeeded', "
            "'fulfillment_retry_scheduled', 'fulfillment_failed', 'compensation_required') "
            "AND entitlement_id IS NOT NULL AND execution_id IS NOT NULL "
            "AND execution_revision >= 1))",
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
        CheckConstraint("id ~ '^fve_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="id_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint("reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'", name="reason_code_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object").ddl_if(
            dialect="postgresql"
        ),
        Index("ix_fulfillment_events_transaction_occurred", "transaction_id", "occurred_at"),
        Index("ix_fulfillment_events_entitlement_occurred", "entitlement_id", "occurred_at"),
        Index("ix_fulfillment_events_execution_revision", "execution_id", "execution_revision"),
        Index("ix_fulfillment_events_type_occurred", "event_type", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_fulfillment_event_id)
    transaction_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("payment_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    entitlement_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("entitlements.id", ondelete="RESTRICT"), nullable=True
    )
    execution_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("fulfillment_executions.id", ondelete="RESTRICT"), nullable=True
    )
    execution_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_type: Mapped[FulfillmentEventType] = mapped_column(
        Enum(
            FulfillmentEventType,
            name="fulfillment_event_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    actor_type: Mapped[FulfillmentEventActorType] = mapped_column(
        Enum(
            FulfillmentEventActorType,
            name="fulfillment_event_actor_type",
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


@event.listens_for(FulfillmentEvent, "before_update")
@event.listens_for(FulfillmentEvent, "before_delete")
def _reject_fulfillment_event_mutation(*_: object) -> None:
    raise InvalidRequestError("Fulfillment events are immutable")
