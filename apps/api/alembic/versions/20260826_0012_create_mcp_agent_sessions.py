"""Create scoped MCP agent sessions and append-only tool audit evidence.

Revision ID: 20260826_0012
Revises: 20260826_0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260826_0012"
down_revision: str | None = "20260826_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "mcp_agent_sessions",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(31),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(71), nullable=False, unique=True),
        sa.Column("scopes", _json(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("expires_at > created_at", name="mcp_agent_session_expiry"),
        sa.CheckConstraint("token_hash LIKE 'sha256:%'", name="mcp_agent_session_token_hash"),
    )
    op.create_index(
        "ix_mcp_agent_sessions_account_created",
        "mcp_agent_sessions",
        ["account_id", "created_at"],
    )
    op.create_index("ix_mcp_agent_sessions_expiry", "mcp_agent_sessions", ["expires_at"])
    op.create_table(
        "mcp_tool_audit_events",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column(
            "agent_session_id",
            sa.String(30),
            sa.ForeignKey("mcp_agent_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.String(31),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("resource_ids", _json(), nullable=False),
        sa.Column("result_code", sa.String(100), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_mcp_tool_audit_account_time",
        "mcp_tool_audit_events",
        ["account_id", "occurred_at"],
    )
    op.create_index(
        "ix_mcp_tool_audit_session_time",
        "mcp_tool_audit_events",
        ["agent_session_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_tool_audit_session_time", table_name="mcp_tool_audit_events")
    op.drop_index("ix_mcp_tool_audit_account_time", table_name="mcp_tool_audit_events")
    op.drop_table("mcp_tool_audit_events")
    op.drop_index("ix_mcp_agent_sessions_expiry", table_name="mcp_agent_sessions")
    op.drop_index("ix_mcp_agent_sessions_account_created", table_name="mcp_agent_sessions")
    op.drop_table("mcp_agent_sessions")
