"""Create short-lived action-bound human-presence proof evidence.

Revision ID: 20260827_0013
Revises: 20260826_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260827_0013"
down_revision: str | None = "20260826_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "human_presence_proofs",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(31),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("session_id_hash", sa.String(71), nullable=False),
        sa.Column(
            "passkey_credential_id",
            sa.String(30),
            sa.ForeignKey("passkey_credentials.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action_class", sa.String(64), nullable=False),
        sa.Column("resource_binding", _json(), nullable=False),
        sa.Column("origin", sa.String(500), nullable=False),
        sa.Column("challenge_hash", sa.String(71), nullable=False, unique=True),
        sa.Column("presence_version", sa.String(10), nullable=False),
        sa.Column("presence_hash", sa.String(71), nullable=False, unique=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("expires_at > issued_at", name="expiry"),
        sa.CheckConstraint("presence_hash LIKE 'sha256:%'", name="hash"),
    )
    op.create_index(
        "ix_human_presence_account_issued", "human_presence_proofs", ["account_id", "issued_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_human_presence_account_issued", table_name="human_presence_proofs")
    op.drop_table("human_presence_proofs")
