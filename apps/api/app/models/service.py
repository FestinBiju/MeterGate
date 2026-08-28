"""Merchant service persistence model."""

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import PurchaseType, ServiceStatus, ServiceType
from app.domain.ids import new_service_id
from app.models.base import Base, JSONObject, TimestampMixin


class Service(TimestampMixin, Base):
    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("merchant_id", "slug", name="uq_services_merchant_id_slug"),
        CheckConstraint("length(trim(name)) > 0", name="service_name_nonblank"),
        CheckConstraint("length(trim(description)) > 0", name="service_description_nonblank"),
        CheckConstraint("length(slug) > 0 AND slug = lower(slug)", name="service_slug_lowercase"),
        CheckConstraint("base_price >= 0", name="service_base_price_nonnegative"),
        CheckConstraint(
            "maximum_fulfillment_seconds BETWEEN 1 AND 86400",
            name="service_fulfillment_seconds_range",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="service_currency_length_uppercase",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'inactive', 'archived')",
            name="service_status",
        ),
        CheckConstraint(
            "service_type IN ('api', 'report', 'dataset', 'inference', 'digital_asset', 'other')",
            name="service_type",
        ),
        CheckConstraint(
            "purchase_type IN ('one_time', 'subscription', 'usage_based')",
            name="purchase_type",
        ),
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="service_slug_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="service_currency_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(input_schema) = 'object'",
            name="service_input_schema_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(output_schema) = 'object'",
            name="service_output_schema_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "output_content_type ~* '^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$'",
            name="service_output_content_type_format",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_services_catalog",
            "status",
            "service_type",
            "purchase_type",
            "created_at",
        ),
        Index(
            "ix_services_merchant_list",
            "merchant_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_service_id)
    merchant_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ServiceStatus] = mapped_column(
        Enum(
            ServiceStatus,
            name="service_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=ServiceStatus.DRAFT,
        server_default=ServiceStatus.DRAFT.value,
    )
    service_type: Mapped[ServiceType] = mapped_column(
        Enum(
            ServiceType,
            name="service_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
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
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    base_price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONObject(), nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONObject(), nullable=False)
    output_content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    maximum_fulfillment_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    refund_on_fulfillment_failure: Mapped[bool] = mapped_column(Boolean, nullable=False)

    merchant: Mapped["Merchant"] = relationship(back_populates="services")


from app.models.merchant import Merchant  # noqa: E402
