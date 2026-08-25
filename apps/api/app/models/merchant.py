"""Merchant persistence model."""

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Enum, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import MerchantStatus
from app.domain.ids import new_merchant_id
from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.service import Service


class Merchant(TimestampMixin, Base):
    __tablename__ = "merchants"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_merchants_slug"),
        CheckConstraint("length(trim(name)) > 0", name="merchant_name_nonblank"),
        CheckConstraint("length(trim(description)) > 0", name="merchant_description_nonblank"),
        CheckConstraint("length(slug) > 0 AND slug = lower(slug)", name="merchant_slug_lowercase"),
        CheckConstraint(
            "status IN ('active', 'inactive', 'suspended')",
            name="merchant_status",
        ),
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="merchant_slug_format",
        ).ddl_if(dialect="postgresql"),
        Index("ix_merchants_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=new_merchant_id)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[MerchantStatus] = mapped_column(
        Enum(
            MerchantStatus,
            name="merchant_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=MerchantStatus.ACTIVE,
        server_default=MerchantStatus.ACTIVE.value,
    )

    services: Mapped[list["Service"]] = relationship(
        back_populates="merchant",
        passive_deletes="all",
    )
