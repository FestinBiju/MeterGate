"""Create append-only operator action evidence.

Revision ID: 20260826_0011
Revises: 20260826_0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260826_0011"
down_revision: str | None = "20260826_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "operator_actions",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column(
            "operator_account_id",
            sa.String(31),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column(
            "transaction_id",
            sa.String(30),
            sa.ForeignKey("payment_transactions.id", ondelete="RESTRICT"),
        ),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("evidence", _json(), nullable=False),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action_hash", sa.String(71), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_operator_actions_idempotency_key"),
    )
    op.create_index(
        "ix_operator_actions_transaction_time",
        "operator_actions",
        ["transaction_id", "acted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_operator_actions_transaction_time", table_name="operator_actions")
    op.drop_table("operator_actions")
