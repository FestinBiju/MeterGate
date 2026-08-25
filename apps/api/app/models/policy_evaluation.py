"""Immutable evidence produced by deterministic policy evaluation."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import PolicyDecision
from app.domain.ids import new_policy_evaluation_id
from app.models.base import Base


class PolicyEvaluation(Base):
    __tablename__ = "policy_evaluations"
    __table_args__ = (
        CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'pye_'",
            name="policy_evaluation_id_length_prefix",
        ),
        CheckConstraint(
            "length(policy_id) = 30 AND substr(policy_id, 1, 4) = 'pol_'",
            name="policy_evaluation_policy_id_length_prefix",
        ),
        CheckConstraint(
            "length(quote_id) = 30 AND substr(quote_id, 1, 4) = 'qte_'",
            name="policy_evaluation_quote_id_length_prefix",
        ),
        CheckConstraint(
            "length(policy_hash) = 71 "
            "AND substr(policy_hash, 1, 7) = 'sha256:' "
            "AND policy_hash = lower(policy_hash)",
            name="policy_evaluation_policy_hash_shape",
        ),
        CheckConstraint(
            "length(quote_hash) = 71 "
            "AND substr(quote_hash, 1, 7) = 'sha256:' "
            "AND quote_hash = lower(quote_hash)",
            name="policy_evaluation_quote_hash_shape",
        ),
        CheckConstraint(
            "decision IN ('allow', 'deny')",
            name="policy_evaluation_decision",
        ),
        CheckConstraint(
            "evaluation_version = '1'",
            name="policy_evaluation_version",
        ),
        CheckConstraint(
            "id ~ '^pye_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="policy_evaluation_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "policy_id ~ '^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="policy_evaluation_policy_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "quote_id ~ '^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="policy_evaluation_quote_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "policy_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="policy_evaluation_policy_hash_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "quote_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="policy_evaluation_quote_hash_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(checks) = 'array' "
            "AND jsonb_array_length(checks) > 0 "
            "AND NOT jsonb_path_exists(checks, "
            "'$[*] ? (@.type() != \"object\")')",
            name="checks_nonempty_object_array",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_policy_evaluations_policy_evaluated_at",
            "policy_id",
            "evaluated_at",
        ),
        Index(
            "ix_policy_evaluations_quote_evaluated_at",
            "quote_id",
            "evaluated_at",
        ),
        Index(
            "ix_policy_evaluations_decision_evaluated_at",
            "decision",
            "evaluated_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
        default=new_policy_evaluation_id,
    )
    policy_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("buyer_policies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quote_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("quotes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    policy_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    quote_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    decision: Mapped[PolicyDecision] = mapped_column(
        Enum(
            PolicyDecision,
            name="policy_decision",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    checks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON(none_as_null=False).with_variant(
            JSONB(none_as_null=False),
            "postgresql",
        ),
        nullable=False,
    )
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluation_version: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


@event.listens_for(PolicyEvaluation, "before_update")
@event.listens_for(PolicyEvaluation, "before_delete")
def _reject_policy_evaluation_mutation(*_: object) -> None:
    raise InvalidRequestError("Policy evaluations are immutable")
