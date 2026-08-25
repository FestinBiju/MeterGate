"""Immutable server-issued quote persistence model."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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

from app.domain.enums import PurchaseType
from app.domain.ids import new_quote_id
from app.models.base import Base, JSONObject


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (
        UniqueConstraint("quote_hash", name="uq_quotes_quote_hash"),
        CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'qte_'",
            name="quote_id_length_prefix",
        ),
        CheckConstraint(
            "amount BETWEEN 0 AND 9007199254740991",
            name="quote_amount_safe_integer_range",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="quote_currency_length_uppercase",
        ),
        CheckConstraint(
            "purchase_type IN ('one_time', 'subscription', 'usage_based')",
            name="purchase_type",
        ),
        CheckConstraint(
            "maximum_fulfillment_seconds BETWEEN 1 AND 86400",
            name="quote_fulfillment_seconds_range",
        ),
        CheckConstraint("expires_at > issued_at", name="quote_expiry_after_issue"),
        CheckConstraint(
            "length(input_hash) = 71 "
            "AND substr(input_hash, 1, 7) = 'sha256:' "
            "AND input_hash = lower(input_hash)",
            name="quote_input_hash_shape",
        ),
        CheckConstraint(
            "length(quote_hash) = 71 "
            "AND substr(quote_hash, 1, 7) = 'sha256:' "
            "AND quote_hash = lower(quote_hash)",
            name="quote_hash_shape",
        ),
        CheckConstraint(
            "id ~ '^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="quote_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="quote_currency_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "input_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="quote_input_hash_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "quote_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="quote_hash_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(service_snapshot) = 'object'",
            name="quote_service_snapshot_object",
        ).ddl_if(dialect="postgresql"),
        Index("ix_quotes_merchant_issued_at", "merchant_id", "issued_at"),
        Index("ix_quotes_service_issued_at", "service_id", "issued_at"),
        Index("ix_quotes_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_quote_id)
    merchant_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    service_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("services.id", ondelete="RESTRICT"),
        nullable=False,
    )
    input: Mapped[Any] = mapped_column(
        JSON(none_as_null=False).with_variant(
            JSONB(none_as_null=False),
            "postgresql",
        ),
        nullable=False,
    )
    input_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    service_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONObject(), nullable=False)
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
    maximum_fulfillment_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    refund_on_fulfillment_failure: Mapped[bool] = mapped_column(Boolean, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    quote_hash: Mapped[str] = mapped_column(String(71), nullable=False)


@event.listens_for(Quote, "before_update")
@event.listens_for(Quote, "before_delete")
def _reject_quote_mutation(*_: object) -> None:
    raise InvalidRequestError("Quotes are immutable; issue a new quote instead")
