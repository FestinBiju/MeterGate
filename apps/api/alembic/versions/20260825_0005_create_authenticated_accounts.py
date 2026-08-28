"""Create authenticated buyer accounts and bind approval identities.

Revision ID: 20260825_0005
Revises: 20260825_0004
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260825_0005"
down_revision: str | None = "20260825_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_postgresql_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION metergate_guard_account_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'accounts cannot be deleted; disable them instead'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'account identity fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.session_version < OLD.session_version THEN
                RAISE EXCEPTION 'account session version cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.updated_at < OLD.updated_at THEN
                RAISE EXCEPTION 'account update time cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                IF NOT (
                    (OLD.status = 'pending' AND NEW.status IN ('active', 'disabled'))
                    OR (OLD.status = 'active' AND NEW.status = 'disabled')
                ) THEN
                    RAISE EXCEPTION 'account status transition is not allowed'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.session_version <= OLD.session_version THEN
                    RAISE EXCEPTION 'account session version must advance on status change'
                        USING ERRCODE = '55000';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_accounts_guard_mutation
        BEFORE UPDATE OR DELETE ON accounts
        FOR EACH ROW
        EXECUTE FUNCTION metergate_guard_account_mutation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION metergate_guard_approval_identity_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'approval identities cannot be deleted; disable them instead'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.account_id IS DISTINCT FROM OLD.account_id
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
        CREATE FUNCTION metergate_require_canonical_approval_identity_subject()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.subject_ref IS DISTINCT FROM NEW.account_id THEN
                RAISE EXCEPTION 'new approval identity subject must equal its account ID'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_approval_identities_canonical_subject
        BEFORE INSERT ON approval_identities
        FOR EACH ROW
        EXECUTE FUNCTION metergate_require_canonical_approval_identity_subject()
        """
    )


def _restore_milestone_five_identity_guard() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION metergate_guard_approval_identity_mutation()
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


def upgrade() -> None:
    account_constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 31 AND substr(id, 1, 5) = 'acct_'",
            name=op.f("ck_accounts_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(display_name) BETWEEN 1 AND 200 AND display_name = trim(display_name)",
            name=op.f("ck_accounts_display_name_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name=op.f("ck_accounts_status"),
        ),
        sa.CheckConstraint(
            "session_version >= 1",
            name=op.f("ck_accounts_session_version_positive"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_accounts_updated_at_valid"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        account_constraints.append(
            sa.CheckConstraint(
                "id ~ '^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                name=op.f("ck_accounts_id_format"),
            )
        )

    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=31), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "active",
                "disabled",
                name="account_status",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "session_version",
            sa.Integer(),
            server_default=sa.text("1"),
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
        *account_constraints,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    op.create_index(
        "ix_accounts_status_created_at",
        "accounts",
        ["status", "created_at"],
        unique=False,
    )

    op.add_column(
        "approval_identities",
        sa.Column("account_id", sa.String(length=31), nullable=True),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO accounts (
                id,
                display_name,
                status,
                session_version,
                created_at,
                updated_at
            )
            SELECT
                'acct_' || substr(identity.id, 5),
                identity.display_name,
                CASE
                    WHEN identity.status = 'disabled' THEN 'disabled'
                    WHEN EXISTS (
                        SELECT 1
                        FROM passkey_credentials AS credential
                        WHERE credential.approval_identity_id = identity.id
                    ) THEN 'active'
                    ELSE 'pending'
                END,
                1,
                identity.created_at,
                identity.updated_at
            FROM approval_identities AS identity
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE approval_identities
            SET account_id = 'acct_' || substr(id, 5)
            """
        )
    )

    with op.batch_alter_table("approval_identities") as batch_op:
        batch_op.alter_column(
            "account_id",
            existing_type=sa.String(length=31),
            nullable=False,
        )
        batch_op.create_check_constraint(
            op.f("ck_approval_identities_account_id_length_prefix"),
            "length(account_id) = 31 AND substr(account_id, 1, 5) = 'acct_'",
        )
        if op.get_context().dialect.name == "postgresql":
            batch_op.create_check_constraint(
                op.f("ck_approval_identities_account_id_format"),
                "account_id ~ '^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            )
        batch_op.create_foreign_key(
            op.f("fk_approval_identities_account"),
            "accounts",
            ["account_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            op.f("uq_approval_identities_account_id"),
            ["account_id"],
        )

    if op.get_context().dialect.name == "postgresql":
        _create_postgresql_guards()


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_approval_identities_canonical_subject "
            "ON approval_identities"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS metergate_require_canonical_approval_identity_subject()"
        )
        op.execute("DROP TRIGGER IF EXISTS trg_accounts_guard_mutation ON accounts")
        op.execute("DROP FUNCTION IF EXISTS metergate_guard_account_mutation()")
        _restore_milestone_five_identity_guard()

    with op.batch_alter_table("approval_identities") as batch_op:
        batch_op.drop_constraint(
            op.f("uq_approval_identities_account_id"),
            type_="unique",
        )
        batch_op.drop_constraint(
            op.f("fk_approval_identities_account"),
            type_="foreignkey",
        )
        if op.get_context().dialect.name == "postgresql":
            batch_op.drop_constraint(
                op.f("ck_approval_identities_account_id_format"),
                type_="check",
            )
        batch_op.drop_constraint(
            op.f("ck_approval_identities_account_id_length_prefix"),
            type_="check",
        )
        batch_op.drop_column("account_id")

    op.drop_index("ix_accounts_status_created_at", table_name="accounts")
    op.drop_table("accounts")
