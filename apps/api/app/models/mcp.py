"""Scoped buyer-agent sessions and append-only MCP tool audit evidence."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, event, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.ids import new_mcp_agent_session_id, new_mcp_audit_event_id
from app.models.base import Base

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class McpAgentSession(Base):
    """Revocable metadata for one short-lived bearer bridge token."""

    __tablename__ = "mcp_agent_sessions"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="mcp_agent_session_expiry"),
        CheckConstraint("token_hash LIKE 'sha256:%'", name="mcp_agent_session_token_hash"),
        Index("ix_mcp_agent_sessions_account_created", "account_id", "created_at"),
        Index("ix_mcp_agent_sessions_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_mcp_agent_session_id)
    account_id: Mapped[str] = mapped_column(
        String(31), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class McpToolAuditEvent(Base):
    """Secret-free immutable evidence of one MCP tool invocation."""

    __tablename__ = "mcp_tool_audit_events"
    __table_args__ = (
        Index("ix_mcp_tool_audit_account_time", "account_id", "occurred_at"),
        Index("ix_mcp_tool_audit_session_time", "agent_session_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_mcp_audit_event_id)
    agent_session_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("mcp_agent_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(31), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_ids: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    result_code: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@event.listens_for(McpToolAuditEvent, "before_update")
@event.listens_for(McpToolAuditEvent, "before_delete")
def _reject_mcp_audit_mutation(*_: object) -> None:
    raise InvalidRequestError("MCP tool audit evidence is append-only")
