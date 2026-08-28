"""Create immutable buyer policies and policy evaluations.

Revision ID: 20260825_0003
Revises: 20260825_0002
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260825_0003"
down_revision: str | None = "20260825_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _policy_allowlist_constraint(column: str, value_pattern: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"CASE WHEN {column} IS NULL THEN TRUE "
        f"WHEN jsonb_typeof({column}) <> 'array' THEN FALSE "
        f"ELSE jsonb_array_length({column}) BETWEEN 1 AND 100 "
        f"AND NOT jsonb_path_exists({column}, "
        "'$[*] ? (@.type() != \"string\")') "
        f"AND NOT jsonb_path_exists({column}, "
        f"'$[*] ? (!(@ like_regex \"{value_pattern}\"))') END",
        name=op.f(f"ck_buyer_policies_policy_{column}_valid_allowlist"),
    )


def upgrade() -> None:
    policy_constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'pol_'",
            name=op.f("ck_buyer_policies_policy_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(subject_ref) BETWEEN 1 AND 200 AND subject_ref = trim(subject_ref)",
            name=op.f("ck_buyer_policies_policy_subject_ref_normalized_nonblank"),
        ),
        sa.CheckConstraint(
            "maximum_amount BETWEEN 0 AND 9007199254740991",
            name=op.f("ck_buyer_policies_policy_maximum_amount_safe_integer_range"),
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name=op.f("ck_buyer_policies_policy_expiry_after_issue"),
        ),
        sa.CheckConstraint(
            "policy_version = '1'",
            name=op.f("ck_buyer_policies_policy_version"),
        ),
        sa.CheckConstraint(
            "length(policy_hash) = 71 "
            "AND substr(policy_hash, 1, 7) = 'sha256:' "
            "AND policy_hash = lower(policy_hash)",
            name=op.f("ck_buyer_policies_policy_hash_shape"),
        ),
    ]
    evaluation_constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'pye_'",
            name=op.f("ck_policy_evaluations_policy_evaluation_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(policy_id) = 30 AND substr(policy_id, 1, 4) = 'pol_'",
            name=op.f("ck_policy_evaluations_policy_evaluation_policy_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(quote_id) = 30 AND substr(quote_id, 1, 4) = 'qte_'",
            name=op.f("ck_policy_evaluations_policy_evaluation_quote_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(policy_hash) = 71 "
            "AND substr(policy_hash, 1, 7) = 'sha256:' "
            "AND policy_hash = lower(policy_hash)",
            name=op.f("ck_policy_evaluations_policy_evaluation_policy_hash_shape"),
        ),
        sa.CheckConstraint(
            "length(quote_hash) = 71 "
            "AND substr(quote_hash, 1, 7) = 'sha256:' "
            "AND quote_hash = lower(quote_hash)",
            name=op.f("ck_policy_evaluations_policy_evaluation_quote_hash_shape"),
        ),
        sa.CheckConstraint(
            "decision IN ('allow', 'deny')",
            name=op.f("ck_policy_evaluations_policy_evaluation_decision"),
        ),
        sa.CheckConstraint(
            "evaluation_version = '1'",
            name=op.f("ck_policy_evaluations_policy_evaluation_version"),
        ),
    ]

    if op.get_context().dialect.name == "postgresql":
        policy_constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_buyer_policies_policy_id_format"),
                ),
                sa.CheckConstraint(
                    "policy_hash ~ '^sha256:[0-9a-f]{64}$'",
                    name=op.f("ck_buyer_policies_policy_hash_format"),
                ),
                _policy_allowlist_constraint("allowed_currencies", "^[A-Z]{3}$"),
                _policy_allowlist_constraint(
                    "allowed_merchant_ids",
                    "^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$",
                ),
                _policy_allowlist_constraint(
                    "allowed_service_ids",
                    "^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$",
                ),
                _policy_allowlist_constraint(
                    "allowed_service_types",
                    "^(api|report|dataset|inference|digital_asset|other)$",
                ),
                _policy_allowlist_constraint(
                    "allowed_purchase_types",
                    "^(one_time|subscription|usage_based)$",
                ),
            )
        )
        evaluation_constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^pye_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_policy_evaluations_policy_evaluation_id_format"),
                ),
                sa.CheckConstraint(
                    "policy_id ~ '^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_policy_evaluations_policy_evaluation_policy_id_format"),
                ),
                sa.CheckConstraint(
                    "quote_id ~ '^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_policy_evaluations_policy_evaluation_quote_id_format"),
                ),
                sa.CheckConstraint(
                    "policy_hash ~ '^sha256:[0-9a-f]{64}$'",
                    name=op.f("ck_policy_evaluations_policy_evaluation_policy_hash_format"),
                ),
                sa.CheckConstraint(
                    "quote_hash ~ '^sha256:[0-9a-f]{64}$'",
                    name=op.f("ck_policy_evaluations_policy_evaluation_quote_hash_format"),
                ),
                sa.CheckConstraint(
                    "jsonb_typeof(checks) = 'array' "
                    "AND jsonb_array_length(checks) > 0 "
                    "AND NOT jsonb_path_exists(checks, "
                    "'$[*] ? (@.type() != \"object\")')",
                    name=op.f("ck_policy_evaluations_checks_nonempty_object_array"),
                ),
            )
        )

    nullable_string_list = sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
        "postgresql",
    )
    op.create_table(
        "buyer_policies",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("subject_ref", sa.String(length=200), nullable=False),
        sa.Column("maximum_amount", sa.BigInteger(), nullable=False),
        sa.Column("allowed_currencies", nullable_string_list, nullable=True),
        sa.Column("allowed_merchant_ids", nullable_string_list, nullable=True),
        sa.Column("allowed_service_ids", nullable_string_list, nullable=True),
        sa.Column("allowed_service_types", nullable_string_list, nullable=True),
        sa.Column("allowed_purchase_types", nullable_string_list, nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version", sa.String(length=16), nullable=False),
        sa.Column("policy_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *policy_constraints,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_buyer_policies")),
        sa.UniqueConstraint(
            "policy_hash",
            name=op.f("uq_buyer_policies_policy_hash"),
        ),
    )
    op.create_index(
        "ix_buyer_policies_subject_issued_at",
        "buyer_policies",
        ["subject_ref", "issued_at"],
        unique=False,
    )
    op.create_index(
        "ix_buyer_policies_expires_at",
        "buyer_policies",
        ["expires_at"],
        unique=False,
    )

    checks_json = sa.JSON(none_as_null=False).with_variant(
        postgresql.JSONB(astext_type=sa.Text(), none_as_null=False),
        "postgresql",
    )
    op.create_table(
        "policy_evaluations",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("policy_id", sa.String(length=30), nullable=False),
        sa.Column("quote_id", sa.String(length=30), nullable=False),
        sa.Column("policy_hash", sa.String(length=71), nullable=False),
        sa.Column("quote_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "decision",
            sa.Enum(
                "allow",
                "deny",
                name="policy_decision",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("checks", checks_json, nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_version", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *evaluation_constraints,
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["buyer_policies.id"],
            name=op.f("fk_policy_evaluations_policy_id_buyer_policies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["quotes.id"],
            name=op.f("fk_policy_evaluations_quote_id_quotes"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_evaluations")),
    )
    op.create_index(
        "ix_policy_evaluations_policy_evaluated_at",
        "policy_evaluations",
        ["policy_id", "evaluated_at"],
        unique=False,
    )
    op.create_index(
        "ix_policy_evaluations_quote_evaluated_at",
        "policy_evaluations",
        ["quote_id", "evaluated_at"],
        unique=False,
    )
    op.create_index(
        "ix_policy_evaluations_decision_evaluated_at",
        "policy_evaluations",
        ["decision", "evaluated_at"],
        unique=False,
    )

    if op.get_context().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION metergate_validate_policy_allowlist_uniqueness()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                candidate_allowlist jsonb;
            BEGIN
                FOREACH candidate_allowlist IN ARRAY ARRAY[
                    NEW.allowed_currencies,
                    NEW.allowed_merchant_ids,
                    NEW.allowed_service_ids,
                    NEW.allowed_service_types,
                    NEW.allowed_purchase_types
                ]
                LOOP
                    IF candidate_allowlist IS NOT NULL
                       AND jsonb_typeof(candidate_allowlist) = 'array'
                       AND jsonb_array_length(candidate_allowlist) <>
                           (
                               SELECT count(DISTINCT item.value)
                               FROM jsonb_array_elements_text(candidate_allowlist)
                                   AS item(value)
                           )
                    THEN
                        RAISE EXCEPTION 'Buyer policy allowlists must contain unique values'
                            USING ERRCODE = '23514';
                    END IF;
                END LOOP;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_buyer_policies_validate_allowlists
            BEFORE INSERT ON buyer_policies
            FOR EACH ROW
            EXECUTE FUNCTION metergate_validate_policy_allowlist_uniqueness()
            """
        )
        op.execute(
            """
            CREATE FUNCTION metergate_reject_policy_record_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION '% rows are immutable; % is not allowed',
                    TG_TABLE_NAME, TG_OP
                    USING ERRCODE = '55000';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_buyer_policies_immutable
            BEFORE UPDATE OR DELETE ON buyer_policies
            FOR EACH ROW
            EXECUTE FUNCTION metergate_reject_policy_record_mutation()
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_policy_evaluations_immutable
            BEFORE UPDATE OR DELETE ON policy_evaluations
            FOR EACH ROW
            EXECUTE FUNCTION metergate_reject_policy_record_mutation()
            """
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_policy_evaluations_immutable ON policy_evaluations")
        op.execute("DROP TRIGGER IF EXISTS trg_buyer_policies_immutable ON buyer_policies")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_buyer_policies_validate_allowlists ON buyer_policies"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_reject_policy_record_mutation()")
        op.execute("DROP FUNCTION IF EXISTS metergate_validate_policy_allowlist_uniqueness()")

    op.drop_index(
        "ix_policy_evaluations_decision_evaluated_at",
        table_name="policy_evaluations",
    )
    op.drop_index(
        "ix_policy_evaluations_quote_evaluated_at",
        table_name="policy_evaluations",
    )
    op.drop_index(
        "ix_policy_evaluations_policy_evaluated_at",
        table_name="policy_evaluations",
    )
    op.drop_table("policy_evaluations")
    op.drop_index("ix_buyer_policies_expires_at", table_name="buyer_policies")
    op.drop_index(
        "ix_buyer_policies_subject_issued_at",
        table_name="buyer_policies",
    )
    op.drop_table("buyer_policies")
