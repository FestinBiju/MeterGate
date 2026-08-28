"""Create immutable quotes.

Revision ID: 20260825_0002
Revises: 20260825_0001
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260825_0002"
down_revision: str | None = "20260825_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    quote_constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'qte_'",
            name=op.f("ck_quotes_quote_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "amount BETWEEN 0 AND 9007199254740991",
            name=op.f("ck_quotes_quote_amount_safe_integer_range"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_quotes_quote_currency_length_uppercase"),
        ),
        sa.CheckConstraint(
            "purchase_type IN ('one_time', 'subscription', 'usage_based')",
            name=op.f("ck_quotes_purchase_type"),
        ),
        sa.CheckConstraint(
            "maximum_fulfillment_seconds BETWEEN 1 AND 86400",
            name=op.f("ck_quotes_quote_fulfillment_seconds_range"),
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name=op.f("ck_quotes_quote_expiry_after_issue"),
        ),
        sa.CheckConstraint(
            "length(input_hash) = 71 "
            "AND substr(input_hash, 1, 7) = 'sha256:' "
            "AND input_hash = lower(input_hash)",
            name=op.f("ck_quotes_quote_input_hash_shape"),
        ),
        sa.CheckConstraint(
            "length(quote_hash) = 71 "
            "AND substr(quote_hash, 1, 7) = 'sha256:' "
            "AND quote_hash = lower(quote_hash)",
            name=op.f("ck_quotes_quote_hash_shape"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        quote_constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_quotes_quote_id_format"),
                ),
                sa.CheckConstraint(
                    "currency ~ '^[A-Z]{3}$'",
                    name=op.f("ck_quotes_quote_currency_format"),
                ),
                sa.CheckConstraint(
                    "input_hash ~ '^sha256:[0-9a-f]{64}$'",
                    name=op.f("ck_quotes_quote_input_hash_format"),
                ),
                sa.CheckConstraint(
                    "quote_hash ~ '^sha256:[0-9a-f]{64}$'",
                    name=op.f("ck_quotes_quote_hash_format"),
                ),
                sa.CheckConstraint(
                    "jsonb_typeof(service_snapshot) = 'object'",
                    name=op.f("ck_quotes_quote_service_snapshot_object"),
                ),
            )
        )

    json_value_type = sa.JSON(none_as_null=False).with_variant(
        postgresql.JSONB(astext_type=sa.Text(), none_as_null=False),
        "postgresql",
    )
    op.create_table(
        "quotes",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("merchant_id", sa.String(length=30), nullable=False),
        sa.Column("service_id", sa.String(length=30), nullable=False),
        sa.Column("input", json_value_type, nullable=False),
        sa.Column("input_hash", sa.String(length=71), nullable=False),
        sa.Column("service_snapshot", json_value_type, nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
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
        sa.Column("maximum_fulfillment_seconds", sa.Integer(), nullable=False),
        sa.Column("refund_on_fulfillment_failure", sa.Boolean(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("quote_hash", sa.String(length=71), nullable=False),
        *quote_constraints,
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_quotes_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_quotes_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quotes")),
        sa.UniqueConstraint("quote_hash", name=op.f("uq_quotes_quote_hash")),
    )
    op.create_index(
        "ix_quotes_merchant_issued_at",
        "quotes",
        ["merchant_id", "issued_at"],
        unique=False,
    )
    op.create_index(
        "ix_quotes_service_issued_at",
        "quotes",
        ["service_id", "issued_at"],
        unique=False,
    )
    op.create_index(
        "ix_quotes_expires_at",
        "quotes",
        ["expires_at"],
        unique=False,
    )

    if op.get_context().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION metergate_reject_quote_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'quotes are immutable; % is not allowed', TG_OP
                    USING ERRCODE = '55000';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_quotes_immutable
            BEFORE UPDATE OR DELETE ON quotes
            FOR EACH ROW
            EXECUTE FUNCTION metergate_reject_quote_mutation()
            """
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_quotes_immutable ON quotes")
        op.execute("DROP FUNCTION IF EXISTS metergate_reject_quote_mutation()")

    op.drop_index("ix_quotes_expires_at", table_name="quotes")
    op.drop_index("ix_quotes_service_issued_at", table_name="quotes")
    op.drop_index("ix_quotes_merchant_issued_at", table_name="quotes")
    op.drop_table("quotes")
