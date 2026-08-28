"""Create merchants and services.

Revision ID: 20260825_0001
Revises:
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260825_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    merchant_constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name=op.f("ck_merchants_merchant_name_nonblank"),
        ),
        sa.CheckConstraint(
            "length(trim(description)) > 0",
            name=op.f("ck_merchants_merchant_description_nonblank"),
        ),
        sa.CheckConstraint(
            "length(slug) > 0 AND slug = lower(slug)",
            name=op.f("ck_merchants_merchant_slug_lowercase"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'suspended')",
            name=op.f("ck_merchants_merchant_status"),
        ),
    ]
    service_constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name=op.f("ck_services_service_name_nonblank"),
        ),
        sa.CheckConstraint(
            "length(trim(description)) > 0",
            name=op.f("ck_services_service_description_nonblank"),
        ),
        sa.CheckConstraint(
            "length(slug) > 0 AND slug = lower(slug)",
            name=op.f("ck_services_service_slug_lowercase"),
        ),
        sa.CheckConstraint(
            "base_price >= 0",
            name=op.f("ck_services_service_base_price_nonnegative"),
        ),
        sa.CheckConstraint(
            "maximum_fulfillment_seconds BETWEEN 1 AND 86400",
            name=op.f("ck_services_service_fulfillment_seconds_range"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_services_service_currency_length_uppercase"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'inactive', 'archived')",
            name=op.f("ck_services_service_status"),
        ),
        sa.CheckConstraint(
            "service_type IN ('api', 'report', 'dataset', 'inference', 'digital_asset', 'other')",
            name=op.f("ck_services_service_type"),
        ),
        sa.CheckConstraint(
            "purchase_type IN ('one_time', 'subscription', 'usage_based')",
            name=op.f("ck_services_purchase_type"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        merchant_constraints.append(
            sa.CheckConstraint(
                "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
                name=op.f("ck_merchants_merchant_slug_format"),
            )
        )
        service_constraints.extend(
            (
                sa.CheckConstraint(
                    "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
                    name=op.f("ck_services_service_slug_format"),
                ),
                sa.CheckConstraint(
                    "currency ~ '^[A-Z]{3}$'",
                    name=op.f("ck_services_service_currency_format"),
                ),
                sa.CheckConstraint(
                    "jsonb_typeof(input_schema) = 'object'",
                    name=op.f("ck_services_service_input_schema_object"),
                ),
                sa.CheckConstraint(
                    "jsonb_typeof(output_schema) = 'object'",
                    name=op.f("ck_services_service_output_schema_object"),
                ),
                sa.CheckConstraint(
                    "output_content_type ~* "
                    "'^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$'",
                    name=op.f("ck_services_service_output_content_type_format"),
                ),
            )
        )

    op.create_table(
        "merchants",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "inactive",
                "suspended",
                name="merchant_status",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *merchant_constraints,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchants")),
        sa.UniqueConstraint("slug", name=op.f("uq_merchants_slug")),
    )
    op.create_index(
        "ix_merchants_status_created_at",
        "merchants",
        ["status", "created_at"],
        unique=False,
    )

    json_object_type = sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()),
        "postgresql",
    )
    op.create_table(
        "services",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("merchant_id", sa.String(length=30), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "active",
                "inactive",
                "archived",
                name="service_status",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column(
            "service_type",
            sa.Enum(
                "api",
                "report",
                "dataset",
                "inference",
                "digital_asset",
                "other",
                name="service_type",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "purchase_type",
            sa.Enum(
                "one_time",
                "subscription",
                "usage_based",
                name="purchase_type",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("base_price", sa.BigInteger(), nullable=False),
        sa.Column("input_schema", json_object_type, nullable=False),
        sa.Column("output_schema", json_object_type, nullable=False),
        sa.Column("output_content_type", sa.String(length=255), nullable=False),
        sa.Column("maximum_fulfillment_seconds", sa.Integer(), nullable=False),
        sa.Column("refund_on_fulfillment_failure", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *service_constraints,
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_services_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
        sa.UniqueConstraint(
            "merchant_id",
            "slug",
            name=op.f("uq_services_merchant_id_slug"),
        ),
    )
    op.create_index(
        "ix_services_catalog",
        "services",
        ["status", "service_type", "purchase_type", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_services_merchant_list",
        "services",
        ["merchant_id", "status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_services_merchant_list", table_name="services")
    op.drop_index("ix_services_catalog", table_name="services")
    op.drop_table("services")
    op.drop_index("ix_merchants_status_created_at", table_name="merchants")
    op.drop_table("merchants")
