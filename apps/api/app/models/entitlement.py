"""Immutable value-release evidence issued from one verified paid transaction."""

from datetime import datetime
from hmac import compare_digest
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
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

from app.domain.entitlement_hashing import recompute_entitlement_integrity
from app.domain.enums import PurchaseType
from app.domain.ids import new_entitlement_id
from app.models.base import Base


def _hash_shape(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"length({column}) = 71 AND substr({column}, 1, 7) = 'sha256:' "
        f"AND {column} = lower({column})",
        name=f"{column}_shape",
    )


def _hash_format(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} ~ '^sha256:[0-9a-f]{{64}}$'",
        name=f"{column}_format",
    ).ddl_if(dialect="postgresql")


class Entitlement(Base):
    """A frozen, hash-bound authorization to execute one paid resource."""

    __tablename__ = "entitlements"
    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_entitlements_transaction_id"),
        UniqueConstraint("entitlement_hash", name="uq_entitlements_entitlement_hash"),
        CheckConstraint("length(id) = 30 AND substr(id, 1, 4) = 'ent_'", name="id_prefix"),
        CheckConstraint(
            "length(account_id) = 31 AND substr(account_id, 1, 5) = 'acct_'",
            name="account_id_prefix",
        ),
        CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name="transaction_id_prefix",
        ),
        CheckConstraint(
            "length(payment_reverification_event_id) = 30 "
            "AND substr(payment_reverification_event_id, 1, 4) = 'pte_'",
            name="reverification_event_id_prefix",
        ),
        CheckConstraint("payment_reverification_revision >= 1", name="reverification_revision"),
        CheckConstraint(
            "length(provider_order_id) BETWEEN 1 AND 64 "
            "AND provider_order_id = trim(provider_order_id)",
            name="provider_order_id_valid",
        ),
        CheckConstraint(
            "length(provider_payment_id) BETWEEN 1 AND 64 "
            "AND provider_payment_id = trim(provider_payment_id)",
            name="provider_payment_id_valid",
        ),
        CheckConstraint("amount BETWEEN 1 AND 9007199254740991", name="amount_safe_integer"),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)", name="currency_uppercase"
        ),
        CheckConstraint(
            "purchase_type IN ('one_time', 'subscription', 'usage_based')",
            name="purchase_type",
        ),
        CheckConstraint("maximum_executions = 1", name="maximum_executions_one"),
        CheckConstraint("expires_at > issued_at", name="expires_after_issue"),
        CheckConstraint("entitlement_version = '1'", name="version"),
        _hash_shape("payment_binding_hash"),
        _hash_shape("authorization_hash"),
        _hash_shape("policy_hash"),
        _hash_shape("quote_hash"),
        _hash_shape("input_hash"),
        _hash_shape("entitlement_hash"),
        CheckConstraint("id ~ '^ent_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="id_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            "provider_order_id ~ '^order_[A-Za-z0-9]+$'", name="provider_order_id_format"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "provider_payment_id ~ '^pay_[A-Za-z0-9]+$'", name="provider_payment_id_format"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format").ddl_if(
            dialect="postgresql"
        ),
        _hash_format("payment_binding_hash"),
        _hash_format("authorization_hash"),
        _hash_format("policy_hash"),
        _hash_format("quote_hash"),
        _hash_format("input_hash"),
        _hash_format("entitlement_hash"),
        Index("ix_entitlements_account_created_at", "account_id", "created_at"),
        Index("ix_entitlements_service_expires_at", "service_id", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_entitlement_id)
    account_id: Mapped[str] = mapped_column(
        String(31), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("payment_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    payment_binding_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    payment_reverification_event_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("payment_transaction_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_reverification_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_payment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("payment_attempts.provider_payment_id", ondelete="RESTRICT"),
        nullable=False,
    )
    authorization_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("purchase_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    authorization_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    evaluation_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("policy_evaluations.id", ondelete="RESTRICT"), nullable=False
    )
    policy_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("buyer_policies.id", ondelete="RESTRICT"), nullable=False
    )
    policy_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    quote_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("quotes.id", ondelete="RESTRICT"), nullable=False
    )
    quote_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    merchant_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    input: Mapped[Any] = mapped_column(
        JSON(none_as_null=False).with_variant(JSONB(none_as_null=False), "postgresql"),
        nullable=False,
    )
    input_hash: Mapped[str] = mapped_column(String(71), nullable=False)
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
    maximum_executions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entitlement_version: Mapped[str] = mapped_column(
        String(16), nullable=False, default="1", server_default="1"
    )
    entitlement_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@event.listens_for(Entitlement, "before_insert")
def _validate_entitlement_insert(
    _mapper: object,
    _connection: object,
    entitlement: Entitlement,
) -> None:
    try:
        integrity = recompute_entitlement_integrity(entitlement)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError("Entitlement integrity fields are invalid") from exc
    if not compare_digest(integrity.input_hash, entitlement.input_hash):
        raise InvalidRequestError("Entitlement input hash does not match its input")
    if not compare_digest(integrity.entitlement_hash, entitlement.entitlement_hash):
        raise InvalidRequestError("Entitlement hash does not match its immutable fields")


@event.listens_for(Entitlement, "before_update")
@event.listens_for(Entitlement, "before_delete")
def _reject_entitlement_mutation(*_: object) -> None:
    raise InvalidRequestError("Entitlements are immutable")
