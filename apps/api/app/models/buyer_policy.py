"""Immutable buyer-defined purchase policy persistence model."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.ids import new_policy_id
from app.models.base import Base

_NULLABLE_STRING_LIST = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True),
    "postgresql",
)


def _jsonb_allowlist_constraint(
    column: str,
    value_pattern: str,
) -> CheckConstraint:
    return CheckConstraint(
        f"CASE WHEN {column} IS NULL THEN TRUE "
        f"WHEN jsonb_typeof({column}) <> 'array' THEN FALSE "
        f"ELSE jsonb_array_length({column}) BETWEEN 1 AND 100 "
        f"AND NOT jsonb_path_exists({column}, "
        "'$[*] ? (@.type() != \"string\")') "
        f"AND NOT jsonb_path_exists({column}, "
        f"'$[*] ? (!(@ like_regex \"{value_pattern}\"))') END",
        name=f"policy_{column}_valid_allowlist",
    ).ddl_if(dialect="postgresql")


class BuyerPolicy(Base):
    __tablename__ = "buyer_policies"
    __table_args__ = (
        UniqueConstraint("policy_hash", name="uq_buyer_policies_policy_hash"),
        CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'pol_'",
            name="policy_id_length_prefix",
        ),
        CheckConstraint(
            "length(subject_ref) BETWEEN 1 AND 200 AND subject_ref = trim(subject_ref)",
            name="policy_subject_ref_normalized_nonblank",
        ),
        CheckConstraint(
            "maximum_amount BETWEEN 0 AND 9007199254740991",
            name="policy_maximum_amount_safe_integer_range",
        ),
        CheckConstraint("expires_at > issued_at", name="policy_expiry_after_issue"),
        CheckConstraint("policy_version = '1'", name="policy_version"),
        CheckConstraint(
            "length(policy_hash) = 71 "
            "AND substr(policy_hash, 1, 7) = 'sha256:' "
            "AND policy_hash = lower(policy_hash)",
            name="policy_hash_shape",
        ),
        CheckConstraint(
            "id ~ '^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="policy_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "policy_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="policy_hash_format",
        ).ddl_if(dialect="postgresql"),
        _jsonb_allowlist_constraint("allowed_currencies", "^[A-Z]{3}$"),
        _jsonb_allowlist_constraint(
            "allowed_merchant_ids",
            "^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$",
        ),
        _jsonb_allowlist_constraint(
            "allowed_service_ids",
            "^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$",
        ),
        _jsonb_allowlist_constraint(
            "allowed_service_types",
            "^(api|report|dataset|inference|digital_asset|other)$",
        ),
        _jsonb_allowlist_constraint(
            "allowed_purchase_types",
            "^(one_time|subscription|usage_based)$",
        ),
        Index("ix_buyer_policies_subject_issued_at", "subject_ref", "issued_at"),
        Index("ix_buyer_policies_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_policy_id)
    subject_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    maximum_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    allowed_currencies: Mapped[list[str] | None] = mapped_column(
        _NULLABLE_STRING_LIST,
        nullable=True,
    )
    allowed_merchant_ids: Mapped[list[str] | None] = mapped_column(
        _NULLABLE_STRING_LIST,
        nullable=True,
    )
    allowed_service_ids: Mapped[list[str] | None] = mapped_column(
        _NULLABLE_STRING_LIST,
        nullable=True,
    )
    allowed_service_types: Mapped[list[str] | None] = mapped_column(
        _NULLABLE_STRING_LIST,
        nullable=True,
    )
    allowed_purchase_types: Mapped[list[str] | None] = mapped_column(
        _NULLABLE_STRING_LIST,
        nullable=True,
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


@event.listens_for(BuyerPolicy, "before_update")
@event.listens_for(BuyerPolicy, "before_delete")
def _reject_policy_mutation(*_: object) -> None:
    raise InvalidRequestError("Buyer policies are immutable; create a new policy instead")
