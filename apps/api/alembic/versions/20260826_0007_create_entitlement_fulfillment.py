"""Create durable entitlement and fulfillment evidence.

Revision ID: 20260826_0007
Revises: 20260825_0006
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260826_0007"
down_revision: str | None = "20260825_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PAYMENT_EVENT_TYPES_0006 = (
    "'payment_transaction_created', 'razorpay_order_creation_started', "
    "'razorpay_order_created', 'razorpay_order_creation_failed', "
    "'razorpay_order_creation_uncertain', 'checkout_signature_verified', "
    "'payment_authorized', 'payment_attempt_failed', 'payment_captured', "
    "'order_paid', 'payment_reconciled', 'reconciliation_required'"
)


def _json_type(*, none_as_null: bool = False) -> sa.types.TypeEngine:
    return sa.JSON(none_as_null=none_as_null).with_variant(
        postgresql.JSONB(astext_type=sa.Text(), none_as_null=none_as_null),
        "postgresql",
    )


def _hash_shape(table: str, column: str, *, nullable: bool = False) -> sa.CheckConstraint:
    expression = (
        f"length({column}) = 71 AND substr({column}, 1, 7) = 'sha256:' "
        f"AND {column} = lower({column})"
    )
    if nullable:
        expression = f"{column} IS NULL OR ({expression})"
    return sa.CheckConstraint(
        expression,
        name=op.f(f"ck_{table}_{column}_shape"),
    )


def _hash_format(table: str, column: str, *, nullable: bool = False) -> sa.CheckConstraint:
    expression = f"{column} ~ '^sha256:[0-9a-f]{{64}}$'"
    if nullable:
        expression = f"{column} IS NULL OR {expression}"
    return sa.CheckConstraint(
        expression,
        name=op.f(f"ck_{table}_{column}_format"),
    )


def _replace_payment_event_type_check(*, include_reverified: bool) -> None:
    allowed = _PAYMENT_EVENT_TYPES_0006
    if include_reverified:
        allowed = f"{allowed}, 'payment_reverified'"
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(
            "payment_transaction_events",
            recreate="always",
        ) as batch_op:
            batch_op.drop_constraint(
                op.f("ck_payment_transaction_events_event_type"),
                type_="check",
            )
            batch_op.create_check_constraint(
                op.f("ck_payment_transaction_events_event_type"),
                f"event_type IN ({allowed})",
            )
        return
    op.drop_constraint(
        op.f("ck_payment_transaction_events_event_type"),
        "payment_transaction_events",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_payment_transaction_events_event_type"),
        "payment_transaction_events",
        f"event_type IN ({allowed})",
    )


def _rewrite_reverified_payment_events_for_0006() -> None:
    """Retain newer evidence in 0006 vocabulary with an explicit provenance marker."""
    is_postgresql = op.get_context().dialect.name == "postgresql"
    if is_postgresql:
        # Migration-time compatibility rewrite only. 0006's append-only guard is
        # restored below before control returns to application code.
        op.execute(
            "DROP TRIGGER IF EXISTS trg_payment_transaction_events_immutable "
            "ON payment_transaction_events"
        )

    payment_events = sa.table(
        "payment_transaction_events",
        sa.column("id", sa.String()),
        sa.column("event_type", sa.String()),
        sa.column("metadata", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(payment_events.c.id, payment_events.c.metadata).where(
            payment_events.c.event_type == "payment_reverified"
        )
    ).mappings()
    for row in rows:
        metadata = dict(row["metadata"] or {})
        compatibility = metadata.get("_metergate_migration")
        if not isinstance(compatibility, dict):
            compatibility = (
                {"preserved_prior_value": compatibility} if compatibility is not None else {}
            )
        else:
            compatibility = dict(compatibility)
        compatibility["20260826_0007_downgrade"] = {
            "event_type_from": "payment_reverified",
            "event_type_to": "payment_reconciled",
        }
        metadata["_metergate_migration"] = compatibility
        connection.execute(
            payment_events.update()
            .where(payment_events.c.id == row["id"])
            .values(event_type="payment_reconciled", metadata=metadata)
        )

    if is_postgresql:
        op.execute(
            """
            CREATE TRIGGER trg_payment_transaction_events_immutable
            BEFORE UPDATE OR DELETE ON payment_transaction_events
            FOR EACH ROW
            EXECUTE FUNCTION metergate_reject_payment_transaction_event_mutation()
            """
        )


def _create_service_fulfillment_configs() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'sfc_'",
            name=op.f("ck_service_fulfillment_configs_id_prefix"),
        ),
        sa.CheckConstraint(
            "provider_type = 'http'",
            name=op.f("ck_service_fulfillment_configs_provider_type"),
        ),
        sa.CheckConstraint(
            "length(endpoint_url) BETWEEN 1 AND 2048 AND endpoint_url = trim(endpoint_url)",
            name=op.f("ck_service_fulfillment_configs_endpoint_url_valid"),
        ),
        sa.CheckConstraint(
            "request_timeout_seconds BETWEEN 1 AND 60",
            name=op.f("ck_service_fulfillment_configs_request_timeout_seconds_range"),
        ),
        sa.CheckConstraint(
            "maximum_attempts BETWEEN 1 AND 10",
            name=op.f("ck_service_fulfillment_configs_maximum_attempts_range"),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_service_fulfillment_configs_revision_positive"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_service_fulfillment_configs_updated_at_valid"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^sfc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_service_fulfillment_configs_id_format"),
                ),
                sa.CheckConstraint(
                    "endpoint_url ~ '^https?://[^[:space:]]+$'",
                    name=op.f("ck_service_fulfillment_configs_endpoint_url_format"),
                ),
            )
        )
    op.create_table(
        "service_fulfillment_configs",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("service_id", sa.String(length=30), nullable=False),
        sa.Column(
            "provider_type",
            sa.Enum(
                "http",
                name="fulfillment_provider_type",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'http'"),
            nullable=False,
        ),
        sa.Column("endpoint_url", sa.String(length=2048), nullable=False),
        sa.Column("request_timeout_seconds", sa.Integer(), server_default="10", nullable=False),
        sa.Column("maximum_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *constraints,
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_service_fulfillment_configs_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_fulfillment_configs")),
        sa.UniqueConstraint("service_id", name=op.f("uq_service_fulfillment_configs_service_id")),
    )


def _create_commerce_outbox_events() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'obx_'",
            name=op.f("ck_commerce_outbox_events_id_prefix"),
        ),
        sa.CheckConstraint(
            "event_type = 'entitlement_issuance_requested'",
            name=op.f("ck_commerce_outbox_events_event_type"),
        ),
        sa.CheckConstraint(
            "aggregate_type = 'payment_transaction'",
            name=op.f("ck_commerce_outbox_events_aggregate_type"),
        ),
        sa.CheckConstraint(
            "length(aggregate_id) = 30 AND substr(aggregate_id, 1, 4) = 'txn_'",
            name=op.f("ck_commerce_outbox_events_aggregate_id_prefix"),
        ),
        sa.CheckConstraint(
            "deduplication_key = 'entitlement:' || aggregate_id",
            name=op.f("ck_commerce_outbox_events_deduplication_binding"),
        ),
        sa.CheckConstraint(
            "payload_version = '1'",
            name=op.f("ck_commerce_outbox_events_payload_version"),
        ),
        sa.CheckConstraint(
            "available_at >= created_at",
            name=op.f("ck_commerce_outbox_events_availability"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_commerce_outbox_events_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "lease_generation >= 0",
            name=op.f("ck_commerce_outbox_events_lease_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "(processing_started_at IS NULL AND lease_expires_at IS NULL) OR "
            "(processing_started_at IS NOT NULL AND lease_expires_at > processing_started_at)",
            name=op.f("ck_commerce_outbox_events_lease_complete"),
        ),
        sa.CheckConstraint(
            "processed_at IS NULL OR processing_started_at IS NOT NULL",
            name=op.f("ck_commerce_outbox_events_processed_claimed"),
        ),
        sa.CheckConstraint(
            "processed_at IS NULL OR processed_at >= processing_started_at",
            name=op.f("ck_commerce_outbox_events_processed_after_claim"),
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR (length(last_error_code) BETWEEN 1 AND 100 "
            "AND last_error_code = trim(last_error_code))",
            name=op.f("ck_commerce_outbox_events_last_error_code_valid"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^obx_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_commerce_outbox_events_id_format"),
                ),
                sa.CheckConstraint(
                    "jsonb_typeof(payload) = 'object'",
                    name=op.f("ck_commerce_outbox_events_payload_object"),
                ),
                sa.CheckConstraint(
                    "last_error_code IS NULL OR last_error_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
                    name=op.f("ck_commerce_outbox_events_last_error_code_format"),
                ),
            )
        )
    op.create_table(
        "commerce_outbox_events",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "entitlement_issuance_requested",
                name="commerce_outbox_event_type",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'entitlement_issuance_requested'"),
            nullable=False,
        ),
        sa.Column(
            "aggregate_type",
            sa.Enum(
                "payment_transaction",
                name="commerce_aggregate_type",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'payment_transaction'"),
            nullable=False,
        ),
        sa.Column("aggregate_id", sa.String(length=30), nullable=False),
        sa.Column("deduplication_key", sa.String(length=128), nullable=False),
        sa.Column("payload_version", sa.String(length=16), server_default="1", nullable=False),
        sa.Column("payload", _json_type(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        *constraints,
        sa.ForeignKeyConstraint(
            ["aggregate_id"],
            ["payment_transactions.id"],
            name=op.f("fk_commerce_outbox_events_aggregate_id_payment_transactions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commerce_outbox_events")),
        sa.UniqueConstraint("deduplication_key", name=op.f("uq_commerce_outbox_events_dedup_key")),
    )
    op.create_index(
        "ix_commerce_outbox_events_due",
        "commerce_outbox_events",
        ["available_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL"),
        sqlite_where=sa.text("processed_at IS NULL"),
    )
    op.create_index(
        "ix_commerce_outbox_events_stale_lease",
        "commerce_outbox_events",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL AND lease_expires_at IS NOT NULL"),
        sqlite_where=sa.text("processed_at IS NULL AND lease_expires_at IS NOT NULL"),
    )
    op.create_index(
        "ix_commerce_outbox_events_aggregate",
        "commerce_outbox_events",
        ["aggregate_type", "aggregate_id"],
        unique=False,
    )


def _create_entitlements() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'ent_'",
            name=op.f("ck_entitlements_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(account_id) = 31 AND substr(account_id, 1, 5) = 'acct_'",
            name=op.f("ck_entitlements_account_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name=op.f("ck_entitlements_transaction_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(payment_reverification_event_id) = 30 "
            "AND substr(payment_reverification_event_id, 1, 4) = 'pte_'",
            name=op.f("ck_entitlements_reverification_event_id_prefix"),
        ),
        sa.CheckConstraint(
            "payment_reverification_revision >= 1",
            name=op.f("ck_entitlements_reverification_revision"),
        ),
        sa.CheckConstraint(
            "length(provider_order_id) BETWEEN 1 AND 64 "
            "AND provider_order_id = trim(provider_order_id)",
            name=op.f("ck_entitlements_provider_order_id_valid"),
        ),
        sa.CheckConstraint(
            "length(provider_payment_id) BETWEEN 1 AND 64 "
            "AND provider_payment_id = trim(provider_payment_id)",
            name=op.f("ck_entitlements_provider_payment_id_valid"),
        ),
        sa.CheckConstraint(
            "amount BETWEEN 1 AND 9007199254740991",
            name=op.f("ck_entitlements_amount_safe_integer"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_entitlements_currency_uppercase"),
        ),
        sa.CheckConstraint(
            "purchase_type IN ('one_time', 'subscription', 'usage_based')",
            name=op.f("ck_entitlements_purchase_type"),
        ),
        sa.CheckConstraint(
            "maximum_executions = 1",
            name=op.f("ck_entitlements_maximum_executions_one"),
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name=op.f("ck_entitlements_expires_after_issue"),
        ),
        sa.CheckConstraint(
            "entitlement_version = '1'",
            name=op.f("ck_entitlements_version"),
        ),
        _hash_shape("entitlements", "payment_binding_hash"),
        _hash_shape("entitlements", "authorization_hash"),
        _hash_shape("entitlements", "policy_hash"),
        _hash_shape("entitlements", "quote_hash"),
        _hash_shape("entitlements", "input_hash"),
        _hash_shape("entitlements", "entitlement_hash"),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^ent_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_entitlements_id_format"),
                ),
                sa.CheckConstraint(
                    "provider_order_id ~ '^order_[A-Za-z0-9]+$'",
                    name=op.f("ck_entitlements_provider_order_id_format"),
                ),
                sa.CheckConstraint(
                    "provider_payment_id ~ '^pay_[A-Za-z0-9]+$'",
                    name=op.f("ck_entitlements_provider_payment_id_format"),
                ),
                sa.CheckConstraint(
                    "currency ~ '^[A-Z]{3}$'",
                    name=op.f("ck_entitlements_currency_format"),
                ),
                _hash_format("entitlements", "payment_binding_hash"),
                _hash_format("entitlements", "authorization_hash"),
                _hash_format("entitlements", "policy_hash"),
                _hash_format("entitlements", "quote_hash"),
                _hash_format("entitlements", "input_hash"),
                _hash_format("entitlements", "entitlement_hash"),
            )
        )
    op.create_table(
        "entitlements",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("account_id", sa.String(length=31), nullable=False),
        sa.Column("transaction_id", sa.String(length=30), nullable=False),
        sa.Column("payment_binding_hash", sa.String(length=71), nullable=False),
        sa.Column("payment_reverification_event_id", sa.String(length=30), nullable=False),
        sa.Column("payment_reverification_revision", sa.BigInteger(), nullable=False),
        sa.Column("provider_order_id", sa.String(length=64), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=64), nullable=False),
        sa.Column("authorization_id", sa.String(length=30), nullable=False),
        sa.Column("authorization_hash", sa.String(length=71), nullable=False),
        sa.Column("evaluation_id", sa.String(length=30), nullable=False),
        sa.Column("policy_id", sa.String(length=30), nullable=False),
        sa.Column("policy_hash", sa.String(length=71), nullable=False),
        sa.Column("quote_id", sa.String(length=30), nullable=False),
        sa.Column("quote_hash", sa.String(length=71), nullable=False),
        sa.Column("merchant_id", sa.String(length=30), nullable=False),
        sa.Column("service_id", sa.String(length=30), nullable=False),
        sa.Column("input", _json_type(), nullable=False),
        sa.Column("input_hash", sa.String(length=71), nullable=False),
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
        sa.Column("maximum_executions", sa.Integer(), server_default="1", nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entitlement_version", sa.String(length=16), server_default="1", nullable=False),
        sa.Column("entitlement_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *constraints,
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_entitlements_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["payment_transactions.id"],
            name=op.f("fk_entitlements_transaction_id_payment_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_reverification_event_id"],
            ["payment_transaction_events.id"],
            name=op.f("fk_entitlements_payment_reverification_event_id_payment_transaction_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_payment_id"],
            ["payment_attempts.provider_payment_id"],
            name=op.f("fk_entitlements_provider_payment_id_payment_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_id"],
            ["purchase_authorizations.id"],
            name=op.f("fk_entitlements_authorization_id_purchase_authorizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["policy_evaluations.id"],
            name=op.f("fk_entitlements_evaluation_id_policy_evaluations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["buyer_policies.id"],
            name=op.f("fk_entitlements_policy_id_buyer_policies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["quotes.id"],
            name=op.f("fk_entitlements_quote_id_quotes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_entitlements_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_entitlements_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entitlements")),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_entitlements_transaction_id")),
        sa.UniqueConstraint("entitlement_hash", name=op.f("uq_entitlements_entitlement_hash")),
    )
    op.create_index(
        "ix_entitlements_account_created_at",
        "entitlements",
        ["account_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_entitlements_service_expires_at",
        "entitlements",
        ["service_id", "expires_at"],
        unique=False,
    )


def _create_fulfillment_executions() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'ful_'",
            name=op.f("ck_fulfillment_executions_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(entitlement_id) = 30 AND substr(entitlement_id, 1, 4) = 'ent_'",
            name=op.f("ck_fulfillment_executions_entitlement_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name=op.f("ck_fulfillment_executions_transaction_id_prefix"),
        ),
        sa.CheckConstraint(
            "execution_state IN ('pending', 'executing', 'retryable_failure', 'succeeded', "
            "'permanent_failure', 'reconciliation_required')",
            name=op.f("ck_fulfillment_executions_execution_state"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_fulfillment_executions_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_fulfillment_executions_revision_positive"),
        ),
        sa.CheckConstraint(
            "lease_generation >= 0",
            name=op.f("ck_fulfillment_executions_lease_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > started_at",
            name=op.f("ck_fulfillment_executions_lease_after_start"),
        ),
        sa.CheckConstraint(
            "result_size_bytes IS NULL OR result_size_bytes BETWEEN 0 AND 1048576",
            name=op.f("ck_fulfillment_executions_result_size_bounded"),
        ),
        sa.CheckConstraint(
            "(result_content_type IS NULL AND result_json IS NULL AND result_hash IS NULL "
            "AND result_size_bytes IS NULL) OR "
            "(result_content_type IS NOT NULL AND result_json IS NOT NULL "
            "AND result_hash IS NOT NULL AND result_size_bytes IS NOT NULL)",
            name=op.f("ck_fulfillment_executions_result_complete"),
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR (length(failure_code) BETWEEN 1 AND 100 "
            "AND failure_code = trim(failure_code))",
            name=op.f("ck_fulfillment_executions_failure_code_valid"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_fulfillment_executions_updated_at_valid"),
        ),
        sa.CheckConstraint(
            "((execution_state = 'pending' AND attempt_count = 0 AND started_at IS NULL "
            "AND completed_at IS NULL AND failed_at IS NULL AND result_hash IS NULL "
            "AND failure_code IS NULL AND NOT compensation_required AND lease_expires_at IS NULL) "
            "OR (execution_state = 'executing' AND attempt_count >= 1 AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND failed_at IS NULL AND result_hash IS NULL "
            "AND failure_code IS NULL AND NOT compensation_required "
            "AND lease_expires_at IS NOT NULL) "
            "OR (execution_state = 'retryable_failure' AND attempt_count >= 1 "
            "AND started_at IS NOT NULL AND completed_at IS NULL AND failed_at IS NOT NULL "
            "AND result_hash IS NULL AND failure_code IS NOT NULL AND NOT compensation_required "
            "AND lease_expires_at IS NULL) "
            "OR (execution_state = 'succeeded' AND attempt_count >= 1 AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND completed_at >= started_at AND failed_at IS NULL "
            "AND result_hash IS NOT NULL AND failure_code IS NULL AND NOT compensation_required "
            "AND lease_expires_at IS NULL) "
            "OR (execution_state = 'permanent_failure' AND attempt_count >= 1 "
            "AND started_at IS NOT NULL AND completed_at IS NULL AND failed_at IS NOT NULL "
            "AND result_hash IS NULL AND failure_code IS NOT NULL AND compensation_required "
            "AND lease_expires_at IS NULL) "
            "OR (execution_state = 'reconciliation_required' AND attempt_count >= 1 "
            "AND started_at IS NOT NULL AND completed_at IS NULL AND failed_at IS NOT NULL "
            "AND result_hash IS NULL AND failure_code IS NOT NULL AND NOT compensation_required "
            "AND lease_expires_at IS NULL))",
            name=op.f("ck_fulfillment_executions_state_payload"),
        ),
        _hash_shape("fulfillment_executions", "input_hash"),
        _hash_shape("fulfillment_executions", "result_hash", nullable=True),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^ful_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_fulfillment_executions_id_format"),
                ),
                _hash_format("fulfillment_executions", "input_hash"),
                _hash_format("fulfillment_executions", "result_hash", nullable=True),
                sa.CheckConstraint(
                    "result_content_type IS NULL OR result_content_type ~* "
                    "'^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$'",
                    name=op.f("ck_fulfillment_executions_result_content_type_format"),
                ),
                sa.CheckConstraint(
                    "failure_code IS NULL OR failure_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
                    name=op.f("ck_fulfillment_executions_failure_code_format"),
                ),
            )
        )
    op.create_table(
        "fulfillment_executions",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("entitlement_id", sa.String(length=30), nullable=False),
        sa.Column("account_id", sa.String(length=31), nullable=False),
        sa.Column("transaction_id", sa.String(length=30), nullable=False),
        sa.Column("merchant_id", sa.String(length=30), nullable=False),
        sa.Column("service_id", sa.String(length=30), nullable=False),
        sa.Column("input_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "execution_state",
            sa.Enum(
                "pending",
                "executing",
                "retryable_failure",
                "succeeded",
                "permanent_failure",
                "reconciliation_required",
                name="fulfillment_execution_state",
                native_enum=False,
                create_constraint=False,
            ),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_content_type", sa.String(length=255), nullable=True),
        sa.Column("result_json", _json_type(none_as_null=True), nullable=True),
        sa.Column("result_hash", sa.String(length=71), nullable=True),
        sa.Column("result_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column(
            "compensation_required", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("lease_generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *constraints,
        sa.ForeignKeyConstraint(
            ["entitlement_id"],
            ["entitlements.id"],
            name=op.f("fk_fulfillment_executions_entitlement_id_entitlements"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_fulfillment_executions_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["payment_transactions.id"],
            name=op.f("fk_fulfillment_executions_transaction_id_payment_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_fulfillment_executions_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_fulfillment_executions_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fulfillment_executions")),
        sa.UniqueConstraint(
            "entitlement_id", name=op.f("uq_fulfillment_executions_entitlement_id")
        ),
    )
    op.create_index(
        "ix_fulfillment_executions_transaction",
        "fulfillment_executions",
        ["transaction_id"],
        unique=False,
    )
    op.create_index(
        "ix_fulfillment_executions_account_created_at",
        "fulfillment_executions",
        ["account_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_fulfillment_executions_state_updated_at",
        "fulfillment_executions",
        ["execution_state", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_fulfillment_executions_lease_expiry",
        "fulfillment_executions",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("execution_state = 'executing'"),
        sqlite_where=sa.text("execution_state = 'executing'"),
    )


def _create_fulfillment_events() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'fve_'",
            name=op.f("ck_fulfillment_events_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name=op.f("ck_fulfillment_events_transaction_id_prefix"),
        ),
        sa.CheckConstraint(
            "entitlement_id IS NULL OR "
            "(length(entitlement_id) = 30 AND substr(entitlement_id, 1, 4) = 'ent_')",
            name=op.f("ck_fulfillment_events_entitlement_id_prefix"),
        ),
        sa.CheckConstraint(
            "execution_id IS NULL OR "
            "(length(execution_id) = 30 AND substr(execution_id, 1, 4) = 'ful_')",
            name=op.f("ck_fulfillment_events_execution_id_prefix"),
        ),
        sa.CheckConstraint(
            "event_type IN ('payment_reverified', 'entitlement_issued', 'capability_issued', "
            "'fulfillment_claimed', 'fulfillment_started', 'merchant_request_sent', "
            "'merchant_response_received', 'fulfillment_succeeded', "
            "'fulfillment_retry_scheduled', 'fulfillment_failed', 'compensation_required')",
            name=op.f("ck_fulfillment_events_event_type"),
        ),
        sa.CheckConstraint(
            "actor_type IN ('account', 'system', 'entitlement_worker', 'resource_gateway', "
            "'merchant_service')",
            name=op.f("ck_fulfillment_events_actor_type"),
        ),
        sa.CheckConstraint(
            "actor_id IS NULL OR (length(actor_id) BETWEEN 1 AND 128 "
            "AND actor_id = trim(actor_id))",
            name=op.f("ck_fulfillment_events_actor_id_valid"),
        ),
        sa.CheckConstraint(
            "((event_type = 'payment_reverified' AND entitlement_id IS NULL "
            "AND execution_id IS NULL AND execution_revision IS NULL) OR "
            "(event_type IN ('entitlement_issued', 'capability_issued') "
            "AND entitlement_id IS NOT NULL AND execution_id IS NULL "
            "AND execution_revision IS NULL) OR "
            "(event_type IN ('fulfillment_claimed', 'fulfillment_started', "
            "'merchant_request_sent', 'merchant_response_received', 'fulfillment_succeeded', "
            "'fulfillment_retry_scheduled', 'fulfillment_failed', 'compensation_required') "
            "AND entitlement_id IS NOT NULL AND execution_id IS NOT NULL "
            "AND execution_revision >= 1))",
            name=op.f("ck_fulfillment_events_event_binding"),
        ),
        sa.CheckConstraint(
            "length(reason_code) BETWEEN 1 AND 100 AND reason_code = trim(reason_code)",
            name=op.f("ck_fulfillment_events_reason_code_valid"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 AND idempotency_key = trim(idempotency_key)",
            name=op.f("ck_fulfillment_events_idempotency_key_valid"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^fve_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_fulfillment_events_id_format"),
                ),
                sa.CheckConstraint(
                    "reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
                    name=op.f("ck_fulfillment_events_reason_code_format"),
                ),
                sa.CheckConstraint(
                    "jsonb_typeof(metadata) = 'object'",
                    name=op.f("ck_fulfillment_events_metadata_object"),
                ),
            )
        )
    op.create_table(
        "fulfillment_events",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("transaction_id", sa.String(length=30), nullable=False),
        sa.Column("entitlement_id", sa.String(length=30), nullable=True),
        sa.Column("execution_id", sa.String(length=30), nullable=True),
        sa.Column("execution_revision", sa.BigInteger(), nullable=True),
        sa.Column(
            "event_type",
            sa.Enum(
                "payment_reverified",
                "entitlement_issued",
                "capability_issued",
                "fulfillment_claimed",
                "fulfillment_started",
                "merchant_request_sent",
                "merchant_response_received",
                "fulfillment_succeeded",
                "fulfillment_retry_scheduled",
                "fulfillment_failed",
                "compensation_required",
                name="fulfillment_event_type",
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
                "entitlement_worker",
                "resource_gateway",
                "merchant_service",
                name="fulfillment_event_actor_type",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("metadata", _json_type(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *constraints,
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["payment_transactions.id"],
            name=op.f("fk_fulfillment_events_transaction_id_payment_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["entitlement_id"],
            ["entitlements.id"],
            name=op.f("fk_fulfillment_events_entitlement_id_entitlements"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["fulfillment_executions.id"],
            name=op.f("fk_fulfillment_events_execution_id_fulfillment_executions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fulfillment_events")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_fulfillment_events_idempotency_key")),
    )
    op.create_index(
        "ix_fulfillment_events_transaction_occurred",
        "fulfillment_events",
        ["transaction_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_fulfillment_events_entitlement_occurred",
        "fulfillment_events",
        ["entitlement_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_fulfillment_events_execution_revision",
        "fulfillment_events",
        ["execution_id", "execution_revision"],
        unique=False,
    )
    op.create_index(
        "ix_fulfillment_events_type_occurred",
        "fulfillment_events",
        ["event_type", "occurred_at"],
        unique=False,
    )


def _create_postgresql_guards() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE FUNCTION metergate_reject_entitlement_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'entitlements are immutable; % is not allowed', TG_OP
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_entitlements_immutable
        BEFORE UPDATE OR DELETE ON entitlements
        FOR EACH ROW EXECUTE FUNCTION metergate_reject_entitlement_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION metergate_guard_commerce_outbox_event()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'commerce outbox events cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF OLD.processed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'processed commerce outbox events are terminal'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(NEW.id, NEW.event_type, NEW.aggregate_type, NEW.aggregate_id,
                   NEW.deduplication_key, NEW.payload_version, NEW.payload, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.event_type, OLD.aggregate_type, OLD.aggregate_id,
                   OLD.deduplication_key, OLD.payload_version, OLD.payload, OLD.created_at) THEN
                RAISE EXCEPTION 'commerce outbox binding and payload are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.attempt_count < OLD.attempt_count
               OR NEW.lease_generation < OLD.lease_generation THEN
                RAISE EXCEPTION 'commerce outbox counters cannot regress' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_commerce_outbox_events_guard
        BEFORE UPDATE OR DELETE ON commerce_outbox_events
        FOR EACH ROW EXECUTE FUNCTION metergate_guard_commerce_outbox_event()
        """
    )
    op.execute(
        """
        CREATE FUNCTION metergate_guard_fulfillment_execution()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'fulfillment executions cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF ROW(NEW.id, NEW.entitlement_id, NEW.account_id, NEW.transaction_id,
                   NEW.merchant_id, NEW.service_id, NEW.input_hash, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.entitlement_id, OLD.account_id, OLD.transaction_id,
                   OLD.merchant_id, OLD.service_id, OLD.input_hash, OLD.created_at) THEN
                RAISE EXCEPTION 'fulfillment execution bindings are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.execution_state = 'succeeded'
               AND ROW(NEW.completed_at, NEW.result_content_type, NEW.result_json,
                       NEW.result_hash, NEW.result_size_bytes)
                   IS DISTINCT FROM
                   ROW(OLD.completed_at, OLD.result_content_type, OLD.result_json,
                       OLD.result_hash, OLD.result_size_bytes) THEN
                RAISE EXCEPTION 'successful fulfillment result evidence is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.execution_state IN ('succeeded', 'permanent_failure')
               AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'terminal fulfillment execution evidence is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.execution_state = 'reconciliation_required'
               AND NEW.execution_state = 'reconciliation_required'
               AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION
                    'fulfillment reconciliation evidence requires an explicit recovery transition'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'fulfillment execution revision must advance exactly once'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.attempt_count < OLD.attempt_count
               OR NEW.lease_generation < OLD.lease_generation THEN
                RAISE EXCEPTION 'fulfillment execution counters cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.execution_state <> OLD.execution_state AND NOT (
                (OLD.execution_state = 'pending' AND NEW.execution_state IN
                    ('executing', 'permanent_failure', 'reconciliation_required')) OR
                (OLD.execution_state = 'executing' AND NEW.execution_state IN
                    ('retryable_failure', 'succeeded', 'permanent_failure',
                     'reconciliation_required')) OR
                (OLD.execution_state = 'retryable_failure' AND NEW.execution_state IN
                    ('executing', 'permanent_failure', 'reconciliation_required')) OR
                (OLD.execution_state = 'reconciliation_required' AND NEW.execution_state IN
                    ('executing', 'succeeded', 'permanent_failure'))
            ) THEN
                RAISE EXCEPTION 'fulfillment execution state transition is not allowed'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_fulfillment_executions_guard
        BEFORE UPDATE OR DELETE ON fulfillment_executions
        FOR EACH ROW EXECUTE FUNCTION metergate_guard_fulfillment_execution()
        """
    )
    op.execute(
        """
        CREATE FUNCTION metergate_reject_fulfillment_event_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'fulfillment events are immutable; % is not allowed', TG_OP
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_fulfillment_events_immutable
        BEFORE UPDATE OR DELETE ON fulfillment_events
        FOR EACH ROW EXECUTE FUNCTION metergate_reject_fulfillment_event_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION metergate_guard_service_fulfillment_config()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'service fulfillment configs cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(NEW.id, NEW.service_id, NEW.created_at) IS DISTINCT FROM
               ROW(OLD.id, OLD.service_id, OLD.created_at) THEN
                RAISE EXCEPTION 'service fulfillment config identity is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'service fulfillment config revision must advance exactly once'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_service_fulfillment_configs_guard
        BEFORE UPDATE OR DELETE ON service_fulfillment_configs
        FOR EACH ROW EXECUTE FUNCTION metergate_guard_service_fulfillment_config()
        """
    )
    op.execute(
        """
        CREATE FUNCTION metergate_require_paid_entitlement_outbox()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.transaction_state = 'paid'
               AND (TG_OP = 'INSERT' OR OLD.transaction_state IS DISTINCT FROM 'paid')
               AND NOT EXISTS (
                   SELECT 1 FROM commerce_outbox_events event
                   WHERE event.event_type = 'entitlement_issuance_requested'
                     AND event.aggregate_type = 'payment_transaction'
                     AND event.aggregate_id = NEW.id
                     AND event.deduplication_key = 'entitlement:' || NEW.id
               ) THEN
                RAISE EXCEPTION 'paid transaction % requires entitlement outbox work', NEW.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'paid_transaction_requires_entitlement_outbox';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_payment_transactions_paid_entitlement_outbox
        AFTER INSERT OR UPDATE OF transaction_state ON payment_transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION metergate_require_paid_entitlement_outbox()
        """
    )


def upgrade() -> None:
    _replace_payment_event_type_check(include_reverified=True)
    _create_service_fulfillment_configs()
    _create_commerce_outbox_events()
    _create_entitlements()
    _create_fulfillment_executions()
    _create_fulfillment_events()
    _create_postgresql_guards()


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_payment_transactions_paid_entitlement_outbox "
            "ON payment_transactions"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_require_paid_entitlement_outbox()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_service_fulfillment_configs_guard "
            "ON service_fulfillment_configs"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_guard_service_fulfillment_config()")
        op.execute("DROP TRIGGER IF EXISTS trg_fulfillment_events_immutable ON fulfillment_events")
        op.execute("DROP FUNCTION IF EXISTS metergate_reject_fulfillment_event_mutation()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_fulfillment_executions_guard ON fulfillment_executions"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_guard_fulfillment_execution()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_commerce_outbox_events_guard ON commerce_outbox_events"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_guard_commerce_outbox_event()")
        op.execute("DROP TRIGGER IF EXISTS trg_entitlements_immutable ON entitlements")
        op.execute("DROP FUNCTION IF EXISTS metergate_reject_entitlement_mutation()")

    op.drop_index("ix_fulfillment_events_type_occurred", table_name="fulfillment_events")
    op.drop_index("ix_fulfillment_events_execution_revision", table_name="fulfillment_events")
    op.drop_index("ix_fulfillment_events_entitlement_occurred", table_name="fulfillment_events")
    op.drop_index("ix_fulfillment_events_transaction_occurred", table_name="fulfillment_events")
    op.drop_table("fulfillment_events")
    op.drop_index("ix_fulfillment_executions_lease_expiry", table_name="fulfillment_executions")
    op.drop_index("ix_fulfillment_executions_state_updated_at", table_name="fulfillment_executions")
    op.drop_index(
        "ix_fulfillment_executions_account_created_at", table_name="fulfillment_executions"
    )
    op.drop_index("ix_fulfillment_executions_transaction", table_name="fulfillment_executions")
    op.drop_table("fulfillment_executions")
    op.drop_index("ix_entitlements_service_expires_at", table_name="entitlements")
    op.drop_index("ix_entitlements_account_created_at", table_name="entitlements")
    op.drop_table("entitlements")
    op.drop_index("ix_commerce_outbox_events_aggregate", table_name="commerce_outbox_events")
    op.drop_index("ix_commerce_outbox_events_stale_lease", table_name="commerce_outbox_events")
    op.drop_index("ix_commerce_outbox_events_due", table_name="commerce_outbox_events")
    op.drop_table("commerce_outbox_events")
    op.drop_table("service_fulfillment_configs")
    _rewrite_reverified_payment_events_for_0006()
    _replace_payment_event_type_check(include_reverified=False)
