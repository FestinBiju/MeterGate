"""Server-authoritative operator roles and append-only operational evidence."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.ids import (
    new_incident_id,
    new_operator_action_id,
    new_operator_decision_id,
    new_operator_role_id,
    new_refund_dispatch_attempt_id,
    new_worker_heartbeat_id,
)
from app.models.base import Base

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class OperatorRole(Base):
    __tablename__ = "operator_roles"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_operator_roles_account_id"),
        CheckConstraint("role IN ('operator','admin')", name="role"),
        CheckConstraint("status IN ('active','disabled')", name="status"),
        Index("ix_operator_roles_status_role", "status", "role"),
    )
    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_operator_role_id)
    account_id: Mapped[str] = mapped_column(
        String(31), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)


class OperatorDecision(Base):
    __tablename__ = "operator_decisions"
    __table_args__ = (
        Index("ix_operator_decisions_case_time", "compensation_case_id", "decided_at"),
    )
    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_operator_decision_id)
    operator_account_id: Mapped[str] = mapped_column(
        String(31), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    compensation_case_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("compensation_cases.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    structured_note: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decision_hash: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)


class OperatorAction(Base):
    """Append-only evidence for non-compensation operator domain actions."""

    __tablename__ = "operator_actions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_operator_actions_idempotency_key"),
        Index("ix_operator_actions_transaction_time", "transaction_id", "acted_at"),
    )
    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_operator_action_id)
    operator_account_id: Mapped[str] = mapped_column(
        String(31), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    transaction_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("payment_transactions.id", ondelete="RESTRICT")
    )
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    acted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_incidents_idempotency_key"),
        CheckConstraint("severity IN ('info','warning','high','critical')", name="severity"),
        CheckConstraint("state IN ('open','acknowledged','resolved')", name="state"),
        Index("ix_incidents_state_severity", "state", "severity", "opened_at"),
    )
    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_incident_id)
    incident_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    transaction_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("payment_transactions.id", ondelete="RESTRICT")
    )
    refund_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("payment_refunds.id", ondelete="RESTRICT")
    )
    fulfillment_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("fulfillment_executions.id", ondelete="RESTRICT")
    )
    compensation_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("compensation_cases.id", ondelete="RESTRICT")
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_operator_id: Mapped[str | None] = mapped_column(
        String(31), ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    summary_code: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)


class RefundDispatchAttempt(Base):
    __tablename__ = "refund_dispatch_attempts"
    __table_args__ = (
        UniqueConstraint("refund_id", "dispatch_generation", name="uq_refund_dispatch_generation"),
        UniqueConstraint("provider_idempotency_key", name="uq_refund_dispatch_idempotency"),
    )
    id: Mapped[str] = mapped_column(
        String(30), primary_key=True, default=new_refund_dispatch_attempt_id
    )
    refund_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("payment_refunds.id", ondelete="RESTRICT"), nullable=False
    )
    dispatch_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    intent_committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    provider_call_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_call_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(100))
    provider_refund_id: Mapped[str | None] = mapped_column(String(100))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        UniqueConstraint("worker_type", "instance_id", name="uq_worker_heartbeat_instance"),
    )
    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_worker_heartbeat_id)
    worker_type: Mapped[str] = mapped_column(String(32), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_successful_work: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backlog_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )


for _model in (OperatorDecision, OperatorAction):
    event.listen(
        _model,
        "before_update",
        lambda *_: (_ for _ in ()).throw(
            InvalidRequestError("Operational evidence is append-only")
        ),
    )
    event.listen(
        _model,
        "before_delete",
        lambda *_: (_ for _ in ()).throw(
            InvalidRequestError("Operational evidence cannot be deleted")
        ),
    )
