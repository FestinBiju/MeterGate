"""Short-lived, action-bound proof that a registered authenticator verified its user."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, event
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.ids import new_human_presence_proof_id
from app.models.base import Base

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class HumanPresenceProof(Base):
    __tablename__ = "human_presence_proofs"
    __table_args__ = (
        CheckConstraint("expires_at > issued_at", name="expiry"),
        CheckConstraint("presence_hash LIKE 'sha256:%'", name="hash"),
        Index("ix_human_presence_account_issued", "account_id", "issued_at"),
    )

    id: Mapped[str] = mapped_column(
        String(30), primary_key=True, default=new_human_presence_proof_id
    )
    account_id: Mapped[str] = mapped_column(
        String(31), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    session_id_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    passkey_credential_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("passkey_credentials.id", ondelete="RESTRICT"), nullable=False
    )
    action_class: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_binding: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    origin: Mapped[str] = mapped_column(String(500), nullable=False)
    challenge_hash: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    presence_version: Mapped[str] = mapped_column(String(10), nullable=False)
    presence_hash: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@event.listens_for(HumanPresenceProof, "before_update")
def _guard_proof_update(_mapper: object, _connection: object, proof: HumanPresenceProof) -> None:
    state = sqlalchemy_inspect(proof)
    immutable = tuple(
        field for field in HumanPresenceProof.__table__.columns.keys() if field != "consumed_at"
    )
    if any(state.attrs[field].history.has_changes() for field in immutable):
        raise InvalidRequestError("Human-presence proof evidence is immutable")
    history = state.attrs.consumed_at.history
    if history.has_changes() and history.deleted and history.deleted[0] is not None:
        raise InvalidRequestError("Human-presence proof consumption is monotonic")


@event.listens_for(HumanPresenceProof, "before_delete")
def _reject_proof_delete(*_: object) -> None:
    raise InvalidRequestError("Human-presence proofs are append-only")
