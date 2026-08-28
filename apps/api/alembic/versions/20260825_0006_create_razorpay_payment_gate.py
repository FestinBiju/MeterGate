"""Create the Razorpay payment transaction gate and append-only evidence.

Revision ID: 20260825_0006
Revises: 20260825_0005
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260825_0006"
down_revision: str | None = "20260825_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _hash_shape_constraint(table: str, column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({column}) = 71 "
        f"AND substr({column}, 1, 7) = 'sha256:' "
        f"AND {column} = lower({column})",
        name=op.f(f"ck_{table}_{column}_shape"),
    )


def _hash_format_constraint(table: str, column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} ~ '^sha256:[0-9a-f]{{64}}$'",
        name=op.f(f"ck_{table}_{column}_format"),
    )


def _create_payment_transactions() -> None:
    table = "payment_transactions"
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'txn_'",
            name=op.f("ck_payment_transactions_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(account_id) = 31 AND substr(account_id, 1, 5) = 'acct_'",
            name=op.f("ck_payment_transactions_account_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(authorization_id) = 30 AND substr(authorization_id, 1, 4) = 'aut_'",
            name=op.f("ck_payment_transactions_authorization_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(evaluation_id) = 30 AND substr(evaluation_id, 1, 4) = 'pye_'",
            name=op.f("ck_payment_transactions_evaluation_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(policy_id) = 30 AND substr(policy_id, 1, 4) = 'pol_'",
            name=op.f("ck_payment_transactions_policy_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(quote_id) = 30 AND substr(quote_id, 1, 4) = 'qte_'",
            name=op.f("ck_payment_transactions_quote_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(merchant_id) = 30 AND substr(merchant_id, 1, 4) = 'mrc_'",
            name=op.f("ck_payment_transactions_merchant_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(service_id) = 30 AND substr(service_id, 1, 4) = 'svc_'",
            name=op.f("ck_payment_transactions_service_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "amount BETWEEN 1 AND 9007199254740991",
            name=op.f("ck_payment_transactions_amount_safe_integer_range"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_payment_transactions_currency_uppercase"),
        ),
        sa.CheckConstraint(
            "purchase_type IN ('one_time', 'subscription', 'usage_based')",
            name=op.f("ck_payment_transactions_purchase_type"),
        ),
        sa.CheckConstraint(
            "provider = 'razorpay'",
            name=op.f("ck_payment_transactions_provider"),
        ),
        sa.CheckConstraint(
            "length(provider_receipt) BETWEEN 1 AND 40 "
            "AND provider_receipt = trim(provider_receipt)",
            name=op.f("ck_payment_transactions_provider_receipt_valid"),
        ),
        sa.CheckConstraint(
            "provider_receipt = id",
            name=op.f("ck_payment_transactions_provider_receipt_is_id"),
        ),
        sa.CheckConstraint(
            "provider_order_id IS NULL OR "
            "(length(provider_order_id) BETWEEN 1 AND 64 "
            "AND provider_order_id = trim(provider_order_id))",
            name=op.f("ck_payment_transactions_provider_order_id_valid"),
        ),
        sa.CheckConstraint(
            "provider_order_status IS NULL "
            "OR provider_order_status IN ('created', 'attempted', 'paid')",
            name=op.f("ck_payment_transactions_provider_order_status"),
        ),
        sa.CheckConstraint(
            "transaction_state IN ("
            "'order_creation_pending', 'order_created', 'order_creation_failed', "
            "'order_creation_uncertain', 'payment_pending', 'payment_authorized', "
            "'paid', 'reconciliation_required')",
            name=op.f("ck_payment_transactions_transaction_state"),
        ),
        sa.CheckConstraint(
            "(provider_order_id IS NULL "
            "AND provider_order_status IS NULL AND order_created_at IS NULL) "
            "OR (provider_order_id IS NOT NULL "
            "AND provider_order_status IS NOT NULL AND order_created_at IS NOT NULL)",
            name=op.f("ck_payment_transactions_provider_order_binding_complete"),
        ),
        sa.CheckConstraint(
            "transaction_state NOT IN ("
            "'order_created', 'payment_pending', 'payment_authorized', 'paid') "
            "OR provider_order_id IS NOT NULL",
            name=op.f("ck_payment_transactions_state_requires_provider_order"),
        ),
        sa.CheckConstraint(
            "(transaction_state = 'paid' AND paid_at IS NOT NULL) "
            "OR (transaction_state <> 'paid' AND paid_at IS NULL)",
            name=op.f("ck_payment_transactions_paid_timestamp_matches_state"),
        ),
        sa.CheckConstraint(
            "order_created_at IS NULL OR order_created_at >= order_creation_started_at",
            name=op.f("ck_payment_transactions_order_created_after_start"),
        ),
        sa.CheckConstraint(
            "paid_at IS NULL OR paid_at >= order_creation_started_at",
            name=op.f("ck_payment_transactions_paid_after_order_start"),
        ),
        sa.CheckConstraint(
            "order_creation_attempts >= 1",
            name=op.f("ck_payment_transactions_order_creation_attempts_positive"),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_payment_transactions_revision_positive"),
        ),
        sa.CheckConstraint(
            "payment_binding_version = '1'",
            name=op.f("ck_payment_transactions_binding_version"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_payment_transactions_updated_at_valid"),
        ),
        _hash_shape_constraint(table, "authorization_hash"),
        _hash_shape_constraint(table, "policy_hash"),
        _hash_shape_constraint(table, "quote_hash"),
        _hash_shape_constraint(table, "payment_binding_hash"),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transactions_id_format"),
                ),
                sa.CheckConstraint(
                    "account_id ~ '^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transactions_account_id_format"),
                ),
                sa.CheckConstraint(
                    "authorization_id ~ '^aut_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transactions_authorization_id_format"),
                ),
                sa.CheckConstraint(
                    "evaluation_id ~ '^pye_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transactions_evaluation_id_format"),
                ),
                sa.CheckConstraint(
                    "policy_id ~ '^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transactions_policy_id_format"),
                ),
                sa.CheckConstraint(
                    "quote_id ~ '^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transactions_quote_id_format"),
                ),
                sa.CheckConstraint(
                    "merchant_id ~ '^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transactions_merchant_id_format"),
                ),
                sa.CheckConstraint(
                    "service_id ~ '^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transactions_service_id_format"),
                ),
                sa.CheckConstraint(
                    "currency ~ '^[A-Z]{3}$'",
                    name=op.f("ck_payment_transactions_currency_format"),
                ),
                sa.CheckConstraint(
                    "provider_order_id IS NULL OR provider_order_id ~ '^order_[A-Za-z0-9]+$'",
                    name=op.f("ck_payment_transactions_provider_order_id_format"),
                ),
                _hash_format_constraint(table, "authorization_hash"),
                _hash_format_constraint(table, "policy_hash"),
                _hash_format_constraint(table, "quote_hash"),
                _hash_format_constraint(table, "payment_binding_hash"),
            )
        )

    op.create_table(
        table,
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("account_id", sa.String(length=31), nullable=False),
        sa.Column("authorization_id", sa.String(length=30), nullable=False),
        sa.Column("authorization_hash", sa.String(length=71), nullable=False),
        sa.Column("evaluation_id", sa.String(length=30), nullable=False),
        sa.Column("policy_id", sa.String(length=30), nullable=False),
        sa.Column("policy_hash", sa.String(length=71), nullable=False),
        sa.Column("quote_id", sa.String(length=30), nullable=False),
        sa.Column("quote_hash", sa.String(length=71), nullable=False),
        sa.Column("merchant_id", sa.String(length=30), nullable=False),
        sa.Column("service_id", sa.String(length=30), nullable=False),
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
        sa.Column(
            "provider",
            sa.Enum(
                "razorpay",
                name="payment_provider",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'razorpay'"),
            nullable=False,
        ),
        sa.Column("provider_receipt", sa.String(length=40), nullable=False),
        sa.Column("provider_order_id", sa.String(length=64), nullable=True),
        sa.Column(
            "provider_order_status",
            sa.Enum(
                "created",
                "attempted",
                "paid",
                name="razorpay_order_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "transaction_state",
            sa.Enum(
                "order_creation_pending",
                "order_created",
                "order_creation_failed",
                "order_creation_uncertain",
                "payment_pending",
                "payment_authorized",
                "paid",
                "reconciliation_required",
                name="payment_transaction_state",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'order_creation_pending'"),
            nullable=False,
        ),
        sa.Column(
            "order_creation_attempts",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("order_creation_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_binding_version", sa.String(length=16), nullable=False),
        sa.Column("payment_binding_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "revision",
            sa.BigInteger(),
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
        *constraints,
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_payment_transactions_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_id"],
            ["purchase_authorizations.id"],
            name=op.f("fk_payment_transactions_authorization_id_purchase_authorizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["policy_evaluations.id"],
            name=op.f("fk_payment_transactions_evaluation_id_policy_evaluations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["buyer_policies.id"],
            name=op.f("fk_payment_transactions_policy_id_buyer_policies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["quotes.id"],
            name=op.f("fk_payment_transactions_quote_id_quotes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_payment_transactions_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_payment_transactions_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_transactions")),
        sa.UniqueConstraint(
            "authorization_id",
            name=op.f("uq_payment_transactions_authorization_id"),
        ),
        sa.UniqueConstraint(
            "provider_receipt",
            name=op.f("uq_payment_transactions_provider_receipt"),
        ),
        sa.UniqueConstraint(
            "provider_order_id",
            name=op.f("uq_payment_transactions_provider_order_id"),
        ),
        sa.UniqueConstraint(
            "payment_binding_hash",
            name=op.f("uq_payment_transactions_payment_binding_hash"),
        ),
        sa.UniqueConstraint(
            "id",
            "provider_order_id",
            name=op.f("uq_payment_transactions_id_provider_order_id"),
        ),
    )
    op.create_index(
        "ix_payment_transactions_account_created_at",
        table,
        ["account_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_transactions_state_updated_at",
        table,
        ["transaction_state", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_transactions_merchant_created_at",
        table,
        ["merchant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_transactions_quote_created_at",
        table,
        ["quote_id", "created_at"],
        unique=False,
    )


def _create_payment_attempts() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'pmt_'",
            name=op.f("ck_payment_attempts_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name=op.f("ck_payment_attempts_transaction_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "provider = 'razorpay'",
            name=op.f("ck_payment_attempts_provider"),
        ),
        sa.CheckConstraint(
            "length(provider_order_id) BETWEEN 1 AND 64 "
            "AND provider_order_id = trim(provider_order_id)",
            name=op.f("ck_payment_attempts_provider_order_id_valid"),
        ),
        sa.CheckConstraint(
            "length(provider_payment_id) BETWEEN 1 AND 64 "
            "AND provider_payment_id = trim(provider_payment_id)",
            name=op.f("ck_payment_attempts_provider_payment_id_valid"),
        ),
        sa.CheckConstraint(
            "amount BETWEEN 1 AND 9007199254740991",
            name=op.f("ck_payment_attempts_amount_safe_integer_range"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_payment_attempts_currency_uppercase"),
        ),
        sa.CheckConstraint(
            "provider_status IN ('created', 'authorized', 'captured', 'failed', 'refunded')",
            name=op.f("ck_payment_attempts_provider_status"),
        ),
        sa.CheckConstraint(
            "method IS NULL OR (length(method) BETWEEN 1 AND 32 AND method = trim(method))",
            name=op.f("ck_payment_attempts_method_valid"),
        ),
        sa.CheckConstraint(
            "(provider_status = 'captured' AND captured) OR provider_status <> 'captured'",
            name=op.f("ck_payment_attempts_captured_status_sets_flag"),
        ),
        sa.CheckConstraint(
            "NOT captured OR provider_status IN ('captured', 'refunded')",
            name=op.f("ck_payment_attempts_captured_flag_matches_status"),
        ),
        sa.CheckConstraint(
            "last_seen_at >= first_seen_at",
            name=op.f("ck_payment_attempts_last_seen_valid"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_payment_attempts_updated_at_valid"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^pmt_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_attempts_id_format"),
                ),
                sa.CheckConstraint(
                    "transaction_id ~ '^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_attempts_transaction_id_format"),
                ),
                sa.CheckConstraint(
                    "provider_order_id ~ '^order_[A-Za-z0-9]+$'",
                    name=op.f("ck_payment_attempts_provider_order_id_format"),
                ),
                sa.CheckConstraint(
                    "provider_payment_id ~ '^pay_[A-Za-z0-9]+$'",
                    name=op.f("ck_payment_attempts_provider_payment_id_format"),
                ),
                sa.CheckConstraint(
                    "currency ~ '^[A-Z]{3}$'",
                    name=op.f("ck_payment_attempts_currency_format"),
                ),
            )
        )

    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("transaction_id", sa.String(length=30), nullable=False),
        sa.Column(
            "provider",
            sa.Enum(
                "razorpay",
                name="payment_provider",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'razorpay'"),
            nullable=False,
        ),
        sa.Column("provider_order_id", sa.String(length=64), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "provider_status",
            sa.Enum(
                "created",
                "authorized",
                "captured",
                "failed",
                "refunded",
                name="payment_attempt_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("method", sa.String(length=32), nullable=True),
        sa.Column(
            "captured",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("provider_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
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
        *constraints,
        sa.ForeignKeyConstraint(
            ["transaction_id", "provider_order_id"],
            ["payment_transactions.id", "payment_transactions.provider_order_id"],
            name=op.f("fk_payment_attempts_transaction_order"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_attempts")),
        sa.UniqueConstraint(
            "provider_payment_id",
            name=op.f("uq_payment_attempts_provider_payment_id"),
        ),
    )
    op.create_index(
        "ix_payment_attempts_transaction_first_seen_at",
        "payment_attempts",
        ["transaction_id", "first_seen_at"],
        unique=False,
    )
    op.create_index(
        "uq_payment_attempts_one_captured_per_transaction",
        "payment_attempts",
        ["transaction_id"],
        unique=True,
        postgresql_where=sa.text("captured"),
        sqlite_where=sa.text("captured = 1"),
    )


def _create_razorpay_webhook_events() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'rwe_'",
            name=op.f("ck_razorpay_webhook_events_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(provider_event_id) BETWEEN 1 AND 128 "
            "AND provider_event_id = trim(provider_event_id)",
            name=op.f("ck_razorpay_webhook_events_provider_event_id_valid"),
        ),
        sa.CheckConstraint(
            "length(provider_event_type) BETWEEN 1 AND 100 "
            "AND provider_event_type = trim(provider_event_type)",
            name=op.f("ck_razorpay_webhook_events_provider_event_type_valid"),
        ),
        sa.CheckConstraint(
            "length(raw_body_hash) = 71 "
            "AND substr(raw_body_hash, 1, 7) = 'sha256:' "
            "AND raw_body_hash = lower(raw_body_hash)",
            name=op.f("ck_razorpay_webhook_events_raw_body_hash_shape"),
        ),
        sa.CheckConstraint(
            "processing_status IN ('processed', 'ignored', 'failed', 'reconciliation_required')",
            name=op.f("ck_razorpay_webhook_events_processing_status"),
        ),
        sa.CheckConstraint(
            "length(processing_reason_code) BETWEEN 1 AND 100 "
            "AND processing_reason_code = trim(processing_reason_code)",
            name=op.f("ck_razorpay_webhook_events_processing_reason_code_valid"),
        ),
        sa.CheckConstraint(
            "provider_order_id IS NULL OR "
            "(length(provider_order_id) BETWEEN 1 AND 64 "
            "AND provider_order_id = trim(provider_order_id))",
            name=op.f("ck_razorpay_webhook_events_provider_order_id_valid"),
        ),
        sa.CheckConstraint(
            "provider_payment_id IS NULL OR "
            "(length(provider_payment_id) BETWEEN 1 AND 64 "
            "AND provider_payment_id = trim(provider_payment_id))",
            name=op.f("ck_razorpay_webhook_events_provider_payment_id_valid"),
        ),
        sa.CheckConstraint(
            "transaction_id IS NULL OR "
            "(length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_')",
            name=op.f("ck_razorpay_webhook_events_transaction_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "processed_at >= received_at",
            name=op.f("ck_razorpay_webhook_events_processed_after_received"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^rwe_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_razorpay_webhook_events_id_format"),
                ),
                sa.CheckConstraint(
                    "raw_body_hash ~ '^sha256:[0-9a-f]{64}$'",
                    name=op.f("ck_razorpay_webhook_events_raw_body_hash_format"),
                ),
                sa.CheckConstraint(
                    "processing_reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
                    name=op.f("ck_razorpay_webhook_events_processing_reason_code_format"),
                ),
                sa.CheckConstraint(
                    "provider_order_id IS NULL OR provider_order_id ~ '^order_[A-Za-z0-9]+$'",
                    name=op.f("ck_razorpay_webhook_events_provider_order_id_format"),
                ),
                sa.CheckConstraint(
                    "provider_payment_id IS NULL OR provider_payment_id ~ '^pay_[A-Za-z0-9]+$'",
                    name=op.f("ck_razorpay_webhook_events_provider_payment_id_format"),
                ),
                sa.CheckConstraint(
                    "transaction_id IS NULL "
                    "OR transaction_id ~ '^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_razorpay_webhook_events_transaction_id_format"),
                ),
            )
        )

    op.create_table(
        "razorpay_webhook_events",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("provider_event_id", sa.String(length=128), nullable=False),
        sa.Column("provider_event_type", sa.String(length=100), nullable=False),
        sa.Column("raw_body_hash", sa.String(length=71), nullable=False),
        sa.Column("provider_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "processing_status",
            sa.Enum(
                "processed",
                "ignored",
                "failed",
                "reconciliation_required",
                name="webhook_processing_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("processing_reason_code", sa.String(length=100), nullable=False),
        sa.Column("provider_order_id", sa.String(length=64), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=64), nullable=True),
        sa.Column("transaction_id", sa.String(length=30), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *constraints,
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["payment_transactions.id"],
            name=op.f("fk_razorpay_webhook_events_transaction_id_payment_transactions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_razorpay_webhook_events")),
        sa.UniqueConstraint(
            "provider_event_id",
            name=op.f("uq_razorpay_webhook_events_provider_event_id"),
        ),
    )
    op.create_index(
        "ix_razorpay_webhook_events_transaction_received_at",
        "razorpay_webhook_events",
        ["transaction_id", "received_at"],
        unique=False,
    )
    op.create_index(
        "ix_razorpay_webhook_events_provider_order_id",
        "razorpay_webhook_events",
        ["provider_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_razorpay_webhook_events_provider_payment_id",
        "razorpay_webhook_events",
        ["provider_payment_id"],
        unique=False,
    )
    op.create_index(
        "ix_razorpay_webhook_events_status_received_at",
        "razorpay_webhook_events",
        ["processing_status", "received_at"],
        unique=False,
    )


def _create_payment_transaction_events() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'pte_'",
            name=op.f("ck_payment_transaction_events_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name=op.f("ck_payment_transaction_events_transaction_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "transaction_revision >= 1",
            name=op.f("ck_payment_transaction_events_transaction_revision_positive"),
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'payment_transaction_created', 'razorpay_order_creation_started', "
            "'razorpay_order_created', 'razorpay_order_creation_failed', "
            "'razorpay_order_creation_uncertain', 'checkout_signature_verified', "
            "'payment_authorized', 'payment_attempt_failed', 'payment_captured', "
            "'order_paid', 'payment_reconciled', 'reconciliation_required')",
            name=op.f("ck_payment_transaction_events_event_type"),
        ),
        sa.CheckConstraint(
            "actor_type IN ('account', 'system', 'provider_api', 'provider_webhook')",
            name=op.f("ck_payment_transaction_events_actor_type"),
        ),
        sa.CheckConstraint(
            "actor_id IS NULL OR "
            "(length(actor_id) BETWEEN 1 AND 128 AND actor_id = trim(actor_id))",
            name=op.f("ck_payment_transaction_events_actor_id_valid"),
        ),
        sa.CheckConstraint(
            "prior_state IS NULL OR prior_state IN ("
            "'order_creation_pending', 'order_created', 'order_creation_failed', "
            "'order_creation_uncertain', 'payment_pending', 'payment_authorized', "
            "'paid', 'reconciliation_required')",
            name=op.f("ck_payment_transaction_events_prior_state"),
        ),
        sa.CheckConstraint(
            "resulting_state IN ("
            "'order_creation_pending', 'order_created', 'order_creation_failed', "
            "'order_creation_uncertain', 'payment_pending', 'payment_authorized', "
            "'paid', 'reconciliation_required')",
            name=op.f("ck_payment_transaction_events_resulting_state"),
        ),
        sa.CheckConstraint(
            "(transaction_revision = 1 AND prior_state IS NULL) "
            "OR (transaction_revision > 1 AND prior_state IS NOT NULL)",
            name=op.f("ck_payment_transaction_events_prior_state_matches_revision"),
        ),
        sa.CheckConstraint(
            "length(reason_code) BETWEEN 1 AND 100 AND reason_code = trim(reason_code)",
            name=op.f("ck_payment_transaction_events_reason_code_valid"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 AND idempotency_key = trim(idempotency_key)",
            name=op.f("ck_payment_transaction_events_idempotency_key_valid"),
        ),
        sa.CheckConstraint(
            "payment_attempt_id IS NULL OR "
            "(length(payment_attempt_id) = 30 "
            "AND substr(payment_attempt_id, 1, 4) = 'pmt_')",
            name=op.f("ck_payment_transaction_events_payment_attempt_id_length_prefix"),
        ),
        sa.CheckConstraint(
            "source_webhook_event_id IS NULL OR "
            "(length(source_webhook_event_id) = 30 "
            "AND substr(source_webhook_event_id, 1, 4) = 'rwe_')",
            name=op.f("ck_payment_transaction_events_source_webhook_id_prefix"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^pte_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transaction_events_id_format"),
                ),
                sa.CheckConstraint(
                    "transaction_id ~ '^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transaction_events_transaction_id_format"),
                ),
                sa.CheckConstraint(
                    "reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
                    name=op.f("ck_payment_transaction_events_reason_code_format"),
                ),
                sa.CheckConstraint(
                    "payment_attempt_id IS NULL "
                    "OR payment_attempt_id ~ '^pmt_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transaction_events_payment_attempt_id_format"),
                ),
                sa.CheckConstraint(
                    "source_webhook_event_id IS NULL "
                    "OR source_webhook_event_id ~ '^rwe_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_transaction_events_source_webhook_event_id_format"),
                ),
                sa.CheckConstraint(
                    "jsonb_typeof(metadata) = 'object'",
                    name=op.f("ck_payment_transaction_events_metadata_object"),
                ),
            )
        )

    metadata_type = sa.JSON(none_as_null=False).with_variant(
        postgresql.JSONB(astext_type=sa.Text(), none_as_null=False),
        "postgresql",
    )
    op.create_table(
        "payment_transaction_events",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("transaction_id", sa.String(length=30), nullable=False),
        sa.Column("transaction_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "payment_transaction_created",
                "razorpay_order_creation_started",
                "razorpay_order_created",
                "razorpay_order_creation_failed",
                "razorpay_order_creation_uncertain",
                "checkout_signature_verified",
                "payment_authorized",
                "payment_attempt_failed",
                "payment_captured",
                "order_paid",
                "payment_reconciled",
                "reconciliation_required",
                name="payment_transaction_event_type",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "actor_type",
            sa.Enum(
                "account",
                "system",
                "provider_api",
                "provider_webhook",
                name="payment_event_actor_type",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column(
            "prior_state",
            sa.Enum(
                "order_creation_pending",
                "order_created",
                "order_creation_failed",
                "order_creation_uncertain",
                "payment_pending",
                "payment_authorized",
                "paid",
                "reconciliation_required",
                name="payment_transaction_state",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "resulting_state",
            sa.Enum(
                "order_creation_pending",
                "order_created",
                "order_creation_failed",
                "order_creation_uncertain",
                "payment_pending",
                "payment_authorized",
                "paid",
                "reconciliation_required",
                name="payment_transaction_state",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("metadata", metadata_type, nullable=False),
        sa.Column("payment_attempt_id", sa.String(length=30), nullable=True),
        sa.Column("source_webhook_event_id", sa.String(length=30), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        *constraints,
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["payment_transactions.id"],
            name=op.f("fk_payment_transaction_events_transaction_id_payment_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_attempt_id"],
            ["payment_attempts.id"],
            name=op.f("fk_payment_transaction_events_payment_attempt_id_payment_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_webhook_event_id"],
            ["razorpay_webhook_events.id"],
            name=op.f(
                "fk_payment_transaction_events_source_webhook_event_id_razorpay_webhook_events"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_transaction_events")),
        sa.UniqueConstraint(
            "transaction_id",
            "transaction_revision",
            name=op.f("uq_payment_transaction_events_transaction_revision"),
        ),
        sa.UniqueConstraint(
            "source_webhook_event_id",
            name=op.f("uq_payment_transaction_events_source_webhook_event_id"),
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_payment_transaction_events_idempotency_key"),
        ),
    )
    op.create_index(
        "ix_payment_transaction_events_transaction_occurred_at",
        "payment_transaction_events",
        ["transaction_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_transaction_events_type_occurred_at",
        "payment_transaction_events",
        ["event_type", "occurred_at"],
        unique=False,
    )


def upgrade() -> None:
    _create_payment_transactions()
    _create_payment_attempts()
    _create_razorpay_webhook_events()
    _create_payment_transaction_events()
    if op.get_context().dialect.name == "postgresql":
        _create_postgresql_guards()


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        _drop_postgresql_guards()

    op.drop_index(
        "ix_payment_transaction_events_type_occurred_at",
        table_name="payment_transaction_events",
    )
    op.drop_index(
        "ix_payment_transaction_events_transaction_occurred_at",
        table_name="payment_transaction_events",
    )
    op.drop_table("payment_transaction_events")
    op.drop_index(
        "ix_razorpay_webhook_events_status_received_at",
        table_name="razorpay_webhook_events",
    )
    op.drop_index(
        "ix_razorpay_webhook_events_provider_payment_id",
        table_name="razorpay_webhook_events",
    )
    op.drop_index(
        "ix_razorpay_webhook_events_provider_order_id",
        table_name="razorpay_webhook_events",
    )
    op.drop_index(
        "ix_razorpay_webhook_events_transaction_received_at",
        table_name="razorpay_webhook_events",
    )
    op.drop_table("razorpay_webhook_events")
    op.drop_index(
        "uq_payment_attempts_one_captured_per_transaction",
        table_name="payment_attempts",
    )
    op.drop_index(
        "ix_payment_attempts_transaction_first_seen_at",
        table_name="payment_attempts",
    )
    op.drop_table("payment_attempts")
    op.drop_index(
        "ix_payment_transactions_quote_created_at",
        table_name="payment_transactions",
    )
    op.drop_index(
        "ix_payment_transactions_merchant_created_at",
        table_name="payment_transactions",
    )
    op.drop_index(
        "ix_payment_transactions_state_updated_at",
        table_name="payment_transactions",
    )
    op.drop_index(
        "ix_payment_transactions_account_created_at",
        table_name="payment_transactions",
    )
    op.drop_table("payment_transactions")


def _create_postgresql_guards() -> None:
    """Install transition, immutability, and mandatory-audit triggers."""
    op.execute(
        """
        CREATE FUNCTION metergate_guard_payment_transaction_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'payment transactions cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.account_id IS DISTINCT FROM OLD.account_id
               OR NEW.authorization_id IS DISTINCT FROM OLD.authorization_id
               OR NEW.authorization_hash IS DISTINCT FROM OLD.authorization_hash
               OR NEW.evaluation_id IS DISTINCT FROM OLD.evaluation_id
               OR NEW.policy_id IS DISTINCT FROM OLD.policy_id
               OR NEW.policy_hash IS DISTINCT FROM OLD.policy_hash
               OR NEW.quote_id IS DISTINCT FROM OLD.quote_id
               OR NEW.quote_hash IS DISTINCT FROM OLD.quote_hash
               OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
               OR NEW.service_id IS DISTINCT FROM OLD.service_id
               OR NEW.amount IS DISTINCT FROM OLD.amount
               OR NEW.currency IS DISTINCT FROM OLD.currency
               OR NEW.purchase_type IS DISTINCT FROM OLD.purchase_type
               OR NEW.provider IS DISTINCT FROM OLD.provider
               OR NEW.provider_receipt IS DISTINCT FROM OLD.provider_receipt
               OR NEW.payment_binding_version IS DISTINCT FROM OLD.payment_binding_version
               OR NEW.payment_binding_hash IS DISTINCT FROM OLD.payment_binding_hash
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'payment transaction binding fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'payment transaction revision must advance exactly once'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.provider_order_id IS NOT NULL
               AND NEW.provider_order_id IS DISTINCT FROM OLD.provider_order_id
            THEN
                RAISE EXCEPTION 'payment provider order binding is immutable once set'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.transaction_state IS DISTINCT FROM OLD.transaction_state
               AND NOT (
                    (OLD.transaction_state = 'order_creation_pending'
                     AND NEW.transaction_state IN (
                        'order_created', 'order_creation_failed',
                        'order_creation_uncertain', 'reconciliation_required'
                     ))
                    OR (OLD.transaction_state = 'order_creation_failed'
                        AND NEW.transaction_state IN (
                            'order_creation_pending', 'reconciliation_required'
                        ))
                    OR (OLD.transaction_state = 'order_creation_uncertain'
                        AND NEW.transaction_state IN (
                            'order_creation_pending', 'order_created',
                            'reconciliation_required'
                        ))
                    OR (OLD.transaction_state = 'order_created'
                        AND NEW.transaction_state IN (
                            'payment_pending', 'payment_authorized', 'paid',
                            'reconciliation_required'
                        ))
                    OR (OLD.transaction_state = 'payment_pending'
                        AND NEW.transaction_state IN (
                            'payment_authorized', 'paid', 'reconciliation_required'
                        ))
                    OR (OLD.transaction_state = 'payment_authorized'
                        AND NEW.transaction_state IN ('paid', 'reconciliation_required'))
                    OR (OLD.transaction_state = 'reconciliation_required'
                        AND NEW.transaction_state IN (
                            'order_creation_pending', 'order_created', 'payment_pending',
                            'payment_authorized', 'paid'
                        ))
               )
            THEN
                RAISE EXCEPTION 'payment transaction state transition is not allowed'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.provider_order_status IS DISTINCT FROM OLD.provider_order_status
               AND NOT (
                    OLD.provider_order_status IS NULL
                    OR (OLD.provider_order_status = 'created'
                        AND NEW.provider_order_status IN ('attempted', 'paid'))
                    OR (OLD.provider_order_status = 'attempted'
                        AND NEW.provider_order_status = 'paid')
               )
            THEN
                RAISE EXCEPTION 'payment provider order status cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.order_creation_attempts < OLD.order_creation_attempts THEN
                RAISE EXCEPTION 'payment order creation attempts cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.order_creation_started_at < OLD.order_creation_started_at THEN
                RAISE EXCEPTION 'payment order creation start time cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.order_created_at IS NOT NULL
               AND NEW.order_created_at IS DISTINCT FROM OLD.order_created_at
            THEN
                RAISE EXCEPTION 'payment order creation time is immutable once recorded'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.paid_at IS NOT NULL
               AND NEW.paid_at IS DISTINCT FROM OLD.paid_at
            THEN
                RAISE EXCEPTION 'payment paid time is immutable once recorded'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.last_reconciled_at IS NOT NULL
               AND (
                    NEW.last_reconciled_at IS NULL
                    OR NEW.last_reconciled_at < OLD.last_reconciled_at
               )
            THEN
                RAISE EXCEPTION 'payment reconciliation time cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.updated_at < OLD.updated_at THEN
                RAISE EXCEPTION 'payment transaction update time cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payment_transactions_guard_mutation
        BEFORE UPDATE OR DELETE ON payment_transactions
        FOR EACH ROW
        EXECUTE FUNCTION metergate_guard_payment_transaction_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION metergate_guard_payment_attempt_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'payment attempts cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.transaction_id IS DISTINCT FROM OLD.transaction_id
               OR NEW.provider IS DISTINCT FROM OLD.provider
               OR NEW.provider_order_id IS DISTINCT FROM OLD.provider_order_id
               OR NEW.provider_payment_id IS DISTINCT FROM OLD.provider_payment_id
               OR NEW.amount IS DISTINCT FROM OLD.amount
               OR NEW.currency IS DISTINCT FROM OLD.currency
               OR NEW.method IS DISTINCT FROM OLD.method
               OR NEW.provider_created_at IS DISTINCT FROM OLD.provider_created_at
               OR NEW.first_seen_at IS DISTINCT FROM OLD.first_seen_at
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'payment attempt binding fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.provider_status IS DISTINCT FROM OLD.provider_status
               AND NOT (
                    (OLD.provider_status = 'created'
                     AND NEW.provider_status IN (
                        'authorized', 'captured', 'failed', 'refunded'
                     ))
                    OR (OLD.provider_status = 'failed'
                        AND NEW.provider_status IN (
                            'authorized', 'captured', 'refunded'
                        ))
                    OR (OLD.provider_status = 'authorized'
                        AND NEW.provider_status IN ('captured', 'refunded'))
                    OR (OLD.provider_status = 'captured'
                        AND NEW.provider_status = 'refunded')
               )
            THEN
                RAISE EXCEPTION 'payment attempt status transition is not allowed'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.captured AND NOT NEW.captured THEN
                RAISE EXCEPTION 'payment attempt captured evidence cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.last_seen_at < OLD.last_seen_at THEN
                RAISE EXCEPTION 'payment attempt last-seen time cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.updated_at < OLD.updated_at THEN
                RAISE EXCEPTION 'payment attempt update time cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payment_attempts_guard_mutation
        BEFORE UPDATE OR DELETE ON payment_attempts
        FOR EACH ROW
        EXECUTE FUNCTION metergate_guard_payment_attempt_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION metergate_reject_razorpay_webhook_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'Razorpay webhook evidence is immutable; % is not allowed', TG_OP
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_razorpay_webhook_events_immutable
        BEFORE UPDATE OR DELETE ON razorpay_webhook_events
        FOR EACH ROW
        EXECUTE FUNCTION metergate_reject_razorpay_webhook_event_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION metergate_reject_payment_transaction_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'payment transaction events are immutable; % is not allowed', TG_OP
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payment_transaction_events_immutable
        BEFORE UPDATE OR DELETE ON payment_transaction_events
        FOR EACH ROW
        EXECUTE FUNCTION metergate_reject_payment_transaction_event_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION metergate_require_payment_transaction_audit_event()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NOT EXISTS (
                    SELECT 1
                    FROM payment_transaction_events AS event
                    WHERE event.transaction_id = NEW.id
                      AND event.transaction_revision = NEW.revision
                      AND event.prior_state IS NULL
                      AND event.resulting_state = NEW.transaction_state
                ) THEN
                    RAISE EXCEPTION 'payment transaction revision lacks audit evidence'
                        USING ERRCODE = '55000';
                END IF;
            ELSE
                IF NOT EXISTS (
                    SELECT 1
                    FROM payment_transaction_events AS event
                    WHERE event.transaction_id = NEW.id
                      AND event.transaction_revision = NEW.revision
                      AND event.prior_state = OLD.transaction_state
                      AND event.resulting_state = NEW.transaction_state
                ) THEN
                    RAISE EXCEPTION 'payment transaction revision lacks audit evidence'
                        USING ERRCODE = '55000';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_payment_transactions_require_audit_event
        AFTER INSERT OR UPDATE ON payment_transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION metergate_require_payment_transaction_audit_event()
        """
    )


def _drop_postgresql_guards() -> None:
    """Drop payment trigger functions before their tables."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_payment_transactions_require_audit_event "
        "ON payment_transactions"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_payment_transaction_events_immutable "
        "ON payment_transaction_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_razorpay_webhook_events_immutable ON razorpay_webhook_events"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_payment_attempts_guard_mutation ON payment_attempts")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_payment_transactions_guard_mutation ON payment_transactions"
    )
    op.execute("DROP FUNCTION IF EXISTS metergate_require_payment_transaction_audit_event()")
    op.execute("DROP FUNCTION IF EXISTS metergate_reject_payment_transaction_event_mutation()")
    op.execute("DROP FUNCTION IF EXISTS metergate_reject_razorpay_webhook_event_mutation()")
    op.execute("DROP FUNCTION IF EXISTS metergate_guard_payment_attempt_mutation()")
    op.execute("DROP FUNCTION IF EXISTS metergate_guard_payment_transaction_mutation()")
