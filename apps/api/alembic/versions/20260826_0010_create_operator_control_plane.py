"""Create operator control-plane evidence and recovery records.

Revision ID: 20260826_0010
Revises: 20260826_0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260826_0010"
down_revision: str | None = "20260826_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "operator_roles",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(31),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("account_id", name="uq_operator_roles_account_id"),
        sa.CheckConstraint("role IN ('operator','admin')", name=op.f("ck_operator_roles_role")),
        sa.CheckConstraint(
            "status IN ('active','disabled')", name=op.f("ck_operator_roles_status")
        ),
    )
    op.create_index("ix_operator_roles_status_role", "operator_roles", ["status", "role"])
    op.create_table(
        "operator_decisions",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column(
            "operator_account_id",
            sa.String(31),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "compensation_case_id",
            sa.String(30),
            sa.ForeignKey("compensation_cases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("structured_note", _json()),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("decision_hash", sa.String(71), nullable=False, unique=True),
    )
    op.create_index(
        "ix_operator_decisions_case_time",
        "operator_decisions",
        ["compensation_case_id", "decided_at"],
    )
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("incident_type", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column(
            "transaction_id",
            sa.String(30),
            sa.ForeignKey("payment_transactions.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "refund_id", sa.String(30), sa.ForeignKey("payment_refunds.id", ondelete="RESTRICT")
        ),
        sa.Column(
            "fulfillment_id",
            sa.String(30),
            sa.ForeignKey("fulfillment_executions.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "compensation_id",
            sa.String(30),
            sa.ForeignKey("compensation_cases.id", ondelete="RESTRICT"),
        ),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "assigned_operator_id", sa.String(31), sa.ForeignKey("accounts.id", ondelete="RESTRICT")
        ),
        sa.Column("summary_code", sa.String(100), nullable=False),
        sa.Column("evidence", _json(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_incidents_idempotency_key"),
        sa.CheckConstraint(
            "severity IN ('info','warning','high','critical')", name=op.f("ck_incidents_severity")
        ),
        sa.CheckConstraint(
            "state IN ('open','acknowledged','resolved')", name=op.f("ck_incidents_state")
        ),
    )
    op.create_index("ix_incidents_state_severity", "incidents", ["state", "severity", "opened_at"])
    op.create_table(
        "refund_dispatch_attempts",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column(
            "refund_id",
            sa.String(30),
            sa.ForeignKey("payment_refunds.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("dispatch_generation", sa.Integer(), nullable=False),
        sa.Column("provider_idempotency_key", sa.String(100), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column(
            "intent_committed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("provider_call_started_at", sa.DateTime(timezone=True)),
        sa.Column("provider_call_finished_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(100)),
        sa.Column("provider_refund_id", sa.String(100)),
        sa.UniqueConstraint(
            "refund_id", "dispatch_generation", name="uq_refund_dispatch_generation"
        ),
        sa.UniqueConstraint("provider_idempotency_key", name="uq_refund_dispatch_idempotency"),
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("worker_type", sa.String(32), nullable=False),
        sa.Column("instance_id", sa.String(64), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_successful_work", sa.DateTime(timezone=True)),
        sa.Column("backlog_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("worker_type", "instance_id", name="uq_worker_heartbeat_instance"),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_table("refund_dispatch_attempts")
    op.drop_index("ix_incidents_state_severity", table_name="incidents")
    op.drop_table("incidents")
    op.drop_index("ix_operator_decisions_case_time", table_name="operator_decisions")
    op.drop_table("operator_decisions")
    op.drop_index("ix_operator_roles_status_role", table_name="operator_roles")
    op.drop_table("operator_roles")
