"""Create trusted approval identities, passkeys, and authorizations.

Revision ID: 20260825_0004
Revises: 20260825_0003
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260825_0004"
down_revision: str | None = "20260825_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _hash_shape_constraint(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({column}) = 71 "
        f"AND substr({column}, 1, 7) = 'sha256:' "
        f"AND {column} = lower({column})",
        name=op.f(f"ck_purchase_authorizations_{column}_shape"),
    )


def _hash_format_constraint(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} ~ '^sha256:[0-9a-f]{{64}}$'",
        name=op.f(f"ck_purchase_authorizations_{column}_format"),
    )


def upgrade() -> None:
    identity_constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'aid_'",
            name=op.f("ck_approval_identities_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(subject_ref) BETWEEN 1 AND 200 AND subject_ref = trim(subject_ref)",
            name=op.f("ck_approval_identities_subject_ref_valid"),
        ),
        sa.CheckConstraint(
            "length(display_name) BETWEEN 1 AND 200 AND display_name = trim(display_name)",
            name=op.f("ck_approval_identities_display_name_valid"),
        ),
        sa.CheckConstraint(
            "length(webauthn_user_handle) = 32",
            name=op.f("ck_approval_identities_webauthn_user_handle_length"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name=op.f("ck_approval_identities_status"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_approval_identities_updated_at_valid"),
        ),
    ]
    credential_constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'pkc_'",
            name=op.f("ck_passkey_credentials_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(approval_identity_id) = 30 AND substr(approval_identity_id, 1, 4) = 'aid_'",
            name=op.f("ck_passkey_credentials_approval_identity_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(credential_id) BETWEEN 1 AND 1024",
            name=op.f("ck_passkey_credentials_credential_id_length"),
        ),
        sa.CheckConstraint(
            "length(public_key) BETWEEN 1 AND 16384",
            name=op.f("ck_passkey_credentials_public_key_length"),
        ),
        sa.CheckConstraint(
            "sign_count BETWEEN 0 AND 4294967295",
            name=op.f("ck_passkey_credentials_sign_count_range"),
        ),
    ]
    authorization_constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'aut_'",
            name=op.f("ck_purchase_authorizations_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(approval_identity_id) = 30 AND substr(approval_identity_id, 1, 4) = 'aid_'",
            name=op.f("ck_purchase_authorizations_approval_identity_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(passkey_credential_id) = 30 AND substr(passkey_credential_id, 1, 4) = 'pkc_'",
            name=op.f("ck_purchase_authorizations_passkey_credential_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(evaluation_id) = 30 AND substr(evaluation_id, 1, 4) = 'pye_'",
            name=op.f("ck_purchase_authorizations_evaluation_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(policy_id) = 30 AND substr(policy_id, 1, 4) = 'pol_'",
            name=op.f("ck_purchase_authorizations_policy_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(quote_id) = 30 AND substr(quote_id, 1, 4) = 'qte_'",
            name=op.f("ck_purchase_authorizations_quote_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(merchant_id) = 30 AND substr(merchant_id, 1, 4) = 'mrc_'",
            name=op.f("ck_purchase_authorizations_merchant_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(service_id) = 30 AND substr(service_id, 1, 4) = 'svc_'",
            name=op.f("ck_purchase_authorizations_service_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(subject_ref) BETWEEN 1 AND 200 AND subject_ref = trim(subject_ref)",
            name=op.f("ck_purchase_authorizations_subject_ref_valid"),
        ),
        sa.CheckConstraint(
            "amount BETWEEN 0 AND 9007199254740991",
            name=op.f("ck_purchase_authorizations_amount_safe_integer_range"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_purchase_authorizations_currency_uppercase"),
        ),
        sa.CheckConstraint(
            "purchase_type IN ('one_time', 'subscription', 'usage_based')",
            name=op.f("ck_purchase_authorizations_purchase_type"),
        ),
        sa.CheckConstraint(
            "expires_at > authorized_at",
            name=op.f("ck_purchase_authorizations_expiry_after_authorization"),
        ),
        sa.CheckConstraint(
            "authorization_version = '1'",
            name=op.f("ck_purchase_authorizations_version"),
        ),
        _hash_shape_constraint("policy_hash"),
        _hash_shape_constraint("quote_hash"),
        _hash_shape_constraint("review_hash"),
        _hash_shape_constraint("challenge_hash"),
        _hash_shape_constraint("authorization_hash"),
    ]

    if op.get_context().dialect.name == "postgresql":
        identity_constraints.append(
            sa.CheckConstraint(
                "id ~ '^aid_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                name=op.f("ck_approval_identities_id_format"),
            )
        )
        credential_constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^pkc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_passkey_credentials_id_format"),
                ),
                sa.CheckConstraint(
                    "approval_identity_id ~ '^aid_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_passkey_credentials_approval_identity_id_format"),
                ),
                sa.CheckConstraint(
                    "CASE WHEN transports IS NULL THEN TRUE "
                    "WHEN jsonb_typeof(transports) <> 'array' THEN FALSE "
                    "ELSE jsonb_array_length(transports) BETWEEN 1 AND 16 "
                    "AND NOT jsonb_path_exists(transports, "
                    "'$[*] ? (@.type() != \"string\")') "
                    "AND NOT jsonb_path_exists(transports, "
                    "'$[*] ? (!(@ like_regex \"^[a-z][a-z0-9-]{0,31}$\"))') END",
                    name=op.f("ck_passkey_credentials_transports_valid"),
                ),
            )
        )
        authorization_constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^aut_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_purchase_authorizations_id_format"),
                ),
                sa.CheckConstraint(
                    "approval_identity_id ~ '^aid_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_purchase_authorizations_approval_identity_id_format"),
                ),
                sa.CheckConstraint(
                    "passkey_credential_id ~ '^pkc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_purchase_authorizations_passkey_credential_id_format"),
                ),
                sa.CheckConstraint(
                    "evaluation_id ~ '^pye_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_purchase_authorizations_evaluation_id_format"),
                ),
                sa.CheckConstraint(
                    "policy_id ~ '^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_purchase_authorizations_policy_id_format"),
                ),
                sa.CheckConstraint(
                    "quote_id ~ '^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_purchase_authorizations_quote_id_format"),
                ),
                sa.CheckConstraint(
                    "merchant_id ~ '^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_purchase_authorizations_merchant_id_format"),
                ),
                sa.CheckConstraint(
                    "service_id ~ '^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_purchase_authorizations_service_id_format"),
                ),
                sa.CheckConstraint(
                    "currency ~ '^[A-Z]{3}$'",
                    name=op.f("ck_purchase_authorizations_currency_format"),
                ),
                _hash_format_constraint("policy_hash"),
                _hash_format_constraint("quote_hash"),
                _hash_format_constraint("review_hash"),
                _hash_format_constraint("challenge_hash"),
                _hash_format_constraint("authorization_hash"),
            )
        )

    op.create_table(
        "approval_identities",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("subject_ref", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("webauthn_user_handle", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "disabled",
                name="approval_identity_status",
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
        *identity_constraints,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_identities")),
        sa.UniqueConstraint(
            "subject_ref",
            name=op.f("uq_approval_identities_subject_ref"),
        ),
        sa.UniqueConstraint(
            "webauthn_user_handle",
            name=op.f("uq_approval_identities_webauthn_user_handle"),
        ),
    )
    op.create_index(
        "ix_approval_identities_status_created_at",
        "approval_identities",
        ["status", "created_at"],
        unique=False,
    )

    transports_type = sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
        "postgresql",
    )
    op.create_table(
        "passkey_credentials",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("approval_identity_id", sa.String(length=30), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(length=1024), nullable=False),
        sa.Column("public_key", sa.LargeBinary(length=16384), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("transports", transports_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *credential_constraints,
        sa.ForeignKeyConstraint(
            ["approval_identity_id"],
            ["approval_identities.id"],
            name=op.f("fk_passkey_credentials_identity"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_passkey_credentials")),
        sa.UniqueConstraint(
            "credential_id",
            name=op.f("uq_passkey_credentials_credential_id"),
        ),
        sa.UniqueConstraint(
            "id",
            "approval_identity_id",
            name=op.f("uq_passkey_credentials_id_approval_identity_id"),
        ),
    )
    op.create_index(
        "ix_passkey_credentials_identity_created_at",
        "passkey_credentials",
        ["approval_identity_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "purchase_authorizations",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("approval_identity_id", sa.String(length=30), nullable=False),
        sa.Column("passkey_credential_id", sa.String(length=30), nullable=False),
        sa.Column("evaluation_id", sa.String(length=30), nullable=False),
        sa.Column("policy_id", sa.String(length=30), nullable=False),
        sa.Column("policy_hash", sa.String(length=71), nullable=False),
        sa.Column("quote_id", sa.String(length=30), nullable=False),
        sa.Column("quote_hash", sa.String(length=71), nullable=False),
        sa.Column("merchant_id", sa.String(length=30), nullable=False),
        sa.Column("service_id", sa.String(length=30), nullable=False),
        sa.Column("subject_ref", sa.String(length=200), nullable=False),
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
        sa.Column("review_hash", sa.String(length=71), nullable=False),
        sa.Column("challenge_hash", sa.String(length=71), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorization_version", sa.String(length=16), nullable=False),
        sa.Column("authorization_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *authorization_constraints,
        sa.ForeignKeyConstraint(
            ["approval_identity_id"],
            ["approval_identities.id"],
            name=op.f("fk_purchase_authorizations_identity"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["passkey_credential_id", "approval_identity_id"],
            ["passkey_credentials.id", "passkey_credentials.approval_identity_id"],
            name=op.f("fk_purchase_authorizations_passkey_identity"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["policy_evaluations.id"],
            name=op.f("fk_purchase_authorizations_evaluation"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["buyer_policies.id"],
            name=op.f("fk_purchase_authorizations_policy"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["quotes.id"],
            name=op.f("fk_purchase_authorizations_quote"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_purchase_authorizations_merchant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_purchase_authorizations_service"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_authorizations")),
        sa.UniqueConstraint(
            "authorization_hash",
            name=op.f("uq_purchase_authorizations_authorization_hash"),
        ),
        sa.UniqueConstraint(
            "challenge_hash",
            name=op.f("uq_purchase_authorizations_challenge_hash"),
        ),
    )
    op.create_index(
        "ix_purchase_authorizations_identity_authorized_at",
        "purchase_authorizations",
        ["approval_identity_id", "authorized_at"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_authorizations_credential_authorized_at",
        "purchase_authorizations",
        ["passkey_credential_id", "authorized_at"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_authorizations_evaluation_authorized_at",
        "purchase_authorizations",
        ["evaluation_id", "authorized_at"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_authorizations_policy_authorized_at",
        "purchase_authorizations",
        ["policy_id", "authorized_at"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_authorizations_quote_authorized_at",
        "purchase_authorizations",
        ["quote_id", "authorized_at"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_authorizations_expires_at",
        "purchase_authorizations",
        ["expires_at"],
        unique=False,
    )

    if op.get_context().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION metergate_guard_approval_identity_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'approval identities cannot be deleted; disable them instead'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.subject_ref IS DISTINCT FROM OLD.subject_ref
                   OR NEW.webauthn_user_handle IS DISTINCT FROM OLD.webauthn_user_handle
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at
                THEN
                    RAISE EXCEPTION 'approval identity binding fields are immutable'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_approval_identities_guard_mutation
            BEFORE UPDATE OR DELETE ON approval_identities
            FOR EACH ROW
            EXECUTE FUNCTION metergate_guard_approval_identity_mutation()
            """
        )
        op.execute(
            """
            CREATE FUNCTION metergate_guard_passkey_credential_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'passkey credentials cannot be deleted'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.approval_identity_id IS DISTINCT FROM OLD.approval_identity_id
                   OR NEW.credential_id IS DISTINCT FROM OLD.credential_id
                   OR NEW.public_key IS DISTINCT FROM OLD.public_key
                   OR NEW.transports IS DISTINCT FROM OLD.transports
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at
                THEN
                    RAISE EXCEPTION 'passkey credential binding fields are immutable'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.sign_count < OLD.sign_count THEN
                    RAISE EXCEPTION 'passkey signature counter cannot regress'
                        USING ERRCODE = '55000';
                END IF;
                IF OLD.last_used_at IS NOT NULL
                   AND (NEW.last_used_at IS NULL OR NEW.last_used_at < OLD.last_used_at)
                THEN
                    RAISE EXCEPTION 'passkey last-used time cannot regress'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_passkey_credentials_guard_mutation
            BEFORE UPDATE OR DELETE ON passkey_credentials
            FOR EACH ROW
            EXECUTE FUNCTION metergate_guard_passkey_credential_mutation()
            """
        )
        op.execute(
            """
            CREATE FUNCTION metergate_reject_purchase_authorization_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'purchase authorizations are immutable; % is not allowed', TG_OP
                    USING ERRCODE = '55000';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_purchase_authorizations_immutable
            BEFORE UPDATE OR DELETE ON purchase_authorizations
            FOR EACH ROW
            EXECUTE FUNCTION metergate_reject_purchase_authorization_mutation()
            """
        )


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_purchase_authorizations_immutable "
            "ON purchase_authorizations"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_passkey_credentials_guard_mutation ON passkey_credentials"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_approval_identities_guard_mutation ON approval_identities"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_reject_purchase_authorization_mutation()")
        op.execute("DROP FUNCTION IF EXISTS metergate_guard_passkey_credential_mutation()")
        op.execute("DROP FUNCTION IF EXISTS metergate_guard_approval_identity_mutation()")

    op.drop_index(
        "ix_purchase_authorizations_expires_at",
        table_name="purchase_authorizations",
    )
    op.drop_index(
        "ix_purchase_authorizations_quote_authorized_at",
        table_name="purchase_authorizations",
    )
    op.drop_index(
        "ix_purchase_authorizations_policy_authorized_at",
        table_name="purchase_authorizations",
    )
    op.drop_index(
        "ix_purchase_authorizations_evaluation_authorized_at",
        table_name="purchase_authorizations",
    )
    op.drop_index(
        "ix_purchase_authorizations_credential_authorized_at",
        table_name="purchase_authorizations",
    )
    op.drop_index(
        "ix_purchase_authorizations_identity_authorized_at",
        table_name="purchase_authorizations",
    )
    op.drop_table("purchase_authorizations")
    op.drop_index(
        "ix_passkey_credentials_identity_created_at",
        table_name="passkey_credentials",
    )
    op.drop_table("passkey_credentials")
    op.drop_index(
        "ix_approval_identities_status_created_at",
        table_name="approval_identities",
    )
    op.drop_table("approval_identities")
