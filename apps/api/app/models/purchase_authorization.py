"""Immutable evidence of one passkey-confirmed purchase authorization."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import PurchaseType
from app.domain.ids import new_authorization_id
from app.models.base import Base


def _hash_shape_constraint(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"length({column}) = 71 "
        f"AND substr({column}, 1, 7) = 'sha256:' "
        f"AND {column} = lower({column})",
        name=f"{column}_shape",
    )


def _hash_format_constraint(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} ~ '^sha256:[0-9a-f]{{64}}$'",
        name=f"{column}_format",
    ).ddl_if(dialect="postgresql")


class PurchaseAuthorization(Base):
    __tablename__ = "purchase_authorizations"
    __table_args__ = (
        UniqueConstraint(
            "authorization_hash",
            name="uq_purchase_authorizations_authorization_hash",
        ),
        UniqueConstraint(
            "challenge_hash",
            name="uq_purchase_authorizations_challenge_hash",
        ),
        ForeignKeyConstraint(
            ["passkey_credential_id", "approval_identity_id"],
            ["passkey_credentials.id", "passkey_credentials.approval_identity_id"],
            name="fk_purchase_authorizations_passkey_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'aut_'",
            name="id_length_prefix",
        ),
        CheckConstraint(
            "length(approval_identity_id) = 30 AND substr(approval_identity_id, 1, 4) = 'aid_'",
            name="approval_identity_id_length_prefix",
        ),
        CheckConstraint(
            "length(passkey_credential_id) = 30 AND substr(passkey_credential_id, 1, 4) = 'pkc_'",
            name="passkey_credential_id_length_prefix",
        ),
        CheckConstraint(
            "length(evaluation_id) = 30 AND substr(evaluation_id, 1, 4) = 'pye_'",
            name="evaluation_id_length_prefix",
        ),
        CheckConstraint(
            "length(policy_id) = 30 AND substr(policy_id, 1, 4) = 'pol_'",
            name="policy_id_length_prefix",
        ),
        CheckConstraint(
            "length(quote_id) = 30 AND substr(quote_id, 1, 4) = 'qte_'",
            name="quote_id_length_prefix",
        ),
        CheckConstraint(
            "length(merchant_id) = 30 AND substr(merchant_id, 1, 4) = 'mrc_'",
            name="merchant_id_length_prefix",
        ),
        CheckConstraint(
            "length(service_id) = 30 AND substr(service_id, 1, 4) = 'svc_'",
            name="service_id_length_prefix",
        ),
        CheckConstraint(
            "length(subject_ref) BETWEEN 1 AND 200 AND subject_ref = trim(subject_ref)",
            name="subject_ref_valid",
        ),
        CheckConstraint(
            "amount BETWEEN 0 AND 9007199254740991",
            name="amount_safe_integer_range",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="currency_uppercase",
        ),
        CheckConstraint(
            "purchase_type IN ('one_time', 'subscription', 'usage_based')",
            name="purchase_type",
        ),
        CheckConstraint(
            "expires_at > authorized_at",
            name="expiry_after_authorization",
        ),
        CheckConstraint(
            "authorization_version = '1'",
            name="version",
        ),
        _hash_shape_constraint("policy_hash"),
        _hash_shape_constraint("quote_hash"),
        _hash_shape_constraint("review_hash"),
        _hash_shape_constraint("challenge_hash"),
        _hash_shape_constraint("authorization_hash"),
        CheckConstraint(
            "id ~ '^aut_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "approval_identity_id ~ '^aid_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="approval_identity_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "passkey_credential_id ~ '^pkc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="passkey_credential_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "evaluation_id ~ '^pye_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="evaluation_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "policy_id ~ '^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="policy_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "quote_id ~ '^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="quote_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "merchant_id ~ '^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="merchant_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "service_id ~ '^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="service_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="currency_format",
        ).ddl_if(dialect="postgresql"),
        _hash_format_constraint("policy_hash"),
        _hash_format_constraint("quote_hash"),
        _hash_format_constraint("review_hash"),
        _hash_format_constraint("challenge_hash"),
        _hash_format_constraint("authorization_hash"),
        Index(
            "ix_purchase_authorizations_identity_authorized_at",
            "approval_identity_id",
            "authorized_at",
        ),
        Index(
            "ix_purchase_authorizations_credential_authorized_at",
            "passkey_credential_id",
            "authorized_at",
        ),
        Index(
            "ix_purchase_authorizations_evaluation_authorized_at",
            "evaluation_id",
            "authorized_at",
        ),
        Index(
            "ix_purchase_authorizations_policy_authorized_at",
            "policy_id",
            "authorized_at",
        ),
        Index(
            "ix_purchase_authorizations_quote_authorized_at",
            "quote_id",
            "authorized_at",
        ),
        Index("ix_purchase_authorizations_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
        default=new_authorization_id,
    )
    approval_identity_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(
            "approval_identities.id",
            name="fk_purchase_authorizations_identity",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    passkey_credential_id: Mapped[str] = mapped_column(String(30), nullable=False)
    evaluation_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(
            "policy_evaluations.id",
            name="fk_purchase_authorizations_evaluation",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    policy_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(
            "buyer_policies.id",
            name="fk_purchase_authorizations_policy",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    policy_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    quote_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(
            "quotes.id",
            name="fk_purchase_authorizations_quote",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    quote_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    merchant_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(
            "merchants.id",
            name="fk_purchase_authorizations_merchant",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    service_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(
            "services.id",
            name="fk_purchase_authorizations_service",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    subject_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    purchase_type: Mapped[PurchaseType] = mapped_column(
        Enum(
            PurchaseType,
            name="purchase_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    review_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    challenge_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorization_version: Mapped[str] = mapped_column(String(16), nullable=False)
    authorization_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


@event.listens_for(PurchaseAuthorization, "before_update")
@event.listens_for(PurchaseAuthorization, "before_delete")
def _reject_purchase_authorization_mutation(*_: object) -> None:
    raise InvalidRequestError("Purchase authorizations are immutable")
