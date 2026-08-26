"""Create compensation decisions, refund ledger, audit, and refund outbox.

Revision ID: 20260826_0009
Revises: 20260826_0008
Create Date: 2026-08-26

Historical permanent failures are deliberately left untouched. This migration
does not infer refund authority or fabricate failure hashes for pre-0009 rows;
application orchestration creates new evidence atomically when it handles a
new terminal failure.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260826_0009"
down_revision: str | None = "20260826_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()),
        "postgresql",
    )


def _create_compensation_cases() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'cmp_'",
            name=op.f("ck_compensation_cases_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(account_id) = 31 AND substr(account_id, 1, 5) = 'acct_'",
            name=op.f("ck_compensation_cases_account_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name=op.f("ck_compensation_cases_transaction_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(payment_attempt_id) = 30 AND substr(payment_attempt_id, 1, 4) = 'pmt_'",
            name=op.f("ck_compensation_cases_payment_attempt_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(entitlement_id) = 30 AND substr(entitlement_id, 1, 4) = 'ent_'",
            name=op.f("ck_compensation_cases_entitlement_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(fulfillment_execution_id) = 30 "
            "AND substr(fulfillment_execution_id, 1, 4) = 'ful_'",
            name=op.f("ck_compensation_cases_fulfillment_execution_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(quote_id) = 30 AND substr(quote_id, 1, 4) = 'qte_'",
            name=op.f("ck_compensation_cases_quote_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(merchant_id) = 30 AND substr(merchant_id, 1, 4) = 'mrc_'",
            name=op.f("ck_compensation_cases_merchant_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(service_id) = 30 AND substr(service_id, 1, 4) = 'svc_'",
            name=op.f("ck_compensation_cases_service_id_prefix"),
        ),
        sa.CheckConstraint(
            "amount_paid BETWEEN 1 AND 9007199254740991",
            name=op.f("ck_compensation_cases_amount_paid_safe_integer"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_compensation_cases_currency_uppercase"),
        ),
        sa.CheckConstraint(
            "length(failure_code) BETWEEN 1 AND 100 AND failure_code = trim(failure_code)",
            name=op.f("ck_compensation_cases_failure_code_valid"),
        ),
        sa.CheckConstraint(
            "failure_evidence_version = '1'",
            name=op.f("ck_compensation_cases_failure_evidence_version"),
        ),
        sa.CheckConstraint(
            "length(failure_evidence_hash) = 71 "
            "AND substr(failure_evidence_hash, 1, 7) = 'sha256:' "
            "AND failure_evidence_hash = lower(failure_evidence_hash)",
            name=op.f("ck_compensation_cases_failure_evidence_hash_shape"),
        ),
        sa.CheckConstraint(
            "length(quote_hash) = 71 AND substr(quote_hash, 1, 7) = 'sha256:' "
            "AND quote_hash = lower(quote_hash)",
            name=op.f("ck_compensation_cases_quote_hash_shape"),
        ),
        sa.CheckConstraint(
            "recommended_action IN ('full_refund', 'manual_review', 'no_refund')",
            name=op.f("ck_compensation_cases_recommended_action"),
        ),
        sa.CheckConstraint(
            "decision_state IN "
            "('pending', 'approved', 'rejected', 'executing', 'completed', 'manual_review')",
            name=op.f("ck_compensation_cases_decision_state"),
        ),
        sa.CheckConstraint(
            "decision_provenance IS NULL OR decision_provenance IN "
            "('automatic_approved', 'manual_approved', 'manual_rejected', 'manual_review')",
            name=op.f("ck_compensation_cases_decision_provenance"),
        ),
        sa.CheckConstraint(
            "approved_refund_amount IS NULL OR approved_refund_amount BETWEEN 1 AND amount_paid",
            name=op.f("ck_compensation_cases_approved_refund_amount_range"),
        ),
        sa.CheckConstraint(
            "decision_reason_code IS NULL OR "
            "(length(decision_reason_code) BETWEEN 1 AND 100 "
            "AND decision_reason_code = trim(decision_reason_code))",
            name=op.f("ck_compensation_cases_decision_reason_code_valid"),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_compensation_cases_revision_positive"),
        ),
        sa.CheckConstraint(
            "decided_at IS NULL OR decided_at >= created_at",
            name=op.f("ck_compensation_cases_decided_after_creation"),
        ),
        sa.CheckConstraint(
            "closed_at IS NULL OR (decided_at IS NOT NULL AND closed_at >= decided_at)",
            name=op.f("ck_compensation_cases_closed_after_decision"),
        ),
        sa.CheckConstraint(
            "((decision_state = 'pending' AND decision_provenance IS NULL "
            "AND approved_refund_amount IS NULL AND decision_reason_code IS NULL "
            "AND decided_at IS NULL AND closed_at IS NULL) OR "
            "(decision_state IN ('approved', 'executing') "
            "AND decision_provenance IN ('automatic_approved', 'manual_approved') "
            "AND approved_refund_amount IS NOT NULL AND decision_reason_code IS NOT NULL "
            "AND decided_at IS NOT NULL AND closed_at IS NULL) OR "
            "(decision_state = 'completed' "
            "AND decision_provenance IN ('automatic_approved', 'manual_approved') "
            "AND approved_refund_amount IS NOT NULL AND decision_reason_code IS NOT NULL "
            "AND decided_at IS NOT NULL AND closed_at IS NOT NULL) OR "
            "(decision_state = 'manual_review' AND "
            "((decision_provenance = 'manual_review' AND approved_refund_amount IS NULL) OR "
            "(decision_provenance IN ('automatic_approved', 'manual_approved') "
            "AND approved_refund_amount IS NOT NULL)) "
            "AND decision_reason_code IS NOT NULL AND decided_at IS NOT NULL "
            "AND closed_at IS NULL) OR "
            "(decision_state = 'rejected' AND decision_provenance = 'manual_rejected' "
            "AND approved_refund_amount IS NULL AND decision_reason_code IS NOT NULL "
            "AND decided_at IS NOT NULL AND closed_at IS NOT NULL))",
            name=op.f("ck_compensation_cases_decision_state_payload"),
        ),
        sa.CheckConstraint(
            "decision_provenance <> 'automatic_approved' OR "
            "(recommended_action = 'full_refund' AND approved_refund_amount = amount_paid)",
            name=op.f("ck_compensation_cases_automatic_full_refund"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^cmp_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_compensation_cases_id_format"),
                ),
                sa.CheckConstraint(
                    "failure_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
                    name=op.f("ck_compensation_cases_failure_code_format"),
                ),
                sa.CheckConstraint(
                    "failure_evidence_hash ~ '^sha256:[0-9a-f]{64}$'",
                    name=op.f("ck_compensation_cases_failure_evidence_hash_format"),
                ),
                sa.CheckConstraint(
                    "quote_hash ~ '^sha256:[0-9a-f]{64}$'",
                    name=op.f("ck_compensation_cases_quote_hash_format"),
                ),
                sa.CheckConstraint(
                    "currency ~ '^[A-Z]{3}$'",
                    name=op.f("ck_compensation_cases_currency_format"),
                ),
                sa.CheckConstraint(
                    "decision_reason_code IS NULL OR "
                    "decision_reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
                    name=op.f("ck_compensation_cases_decision_reason_code_format"),
                ),
            )
        )
    op.create_table(
        "compensation_cases",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("account_id", sa.String(length=31), nullable=False),
        sa.Column("transaction_id", sa.String(length=30), nullable=False),
        sa.Column("payment_attempt_id", sa.String(length=30), nullable=False),
        sa.Column("entitlement_id", sa.String(length=30), nullable=False),
        sa.Column("fulfillment_execution_id", sa.String(length=30), nullable=False),
        sa.Column("quote_id", sa.String(length=30), nullable=False),
        sa.Column("quote_hash", sa.String(length=71), nullable=False),
        sa.Column("merchant_id", sa.String(length=30), nullable=False),
        sa.Column("service_id", sa.String(length=30), nullable=False),
        sa.Column("amount_paid", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=False),
        sa.Column(
            "failure_evidence_version",
            sa.String(length=16),
            server_default="1",
            nullable=False,
        ),
        sa.Column("failure_evidence_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "recommended_action",
            sa.Enum(
                "full_refund",
                "manual_review",
                "no_refund",
                name="compensation_recommended_action",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "decision_state",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                "executing",
                "completed",
                "manual_review",
                name="compensation_decision_state",
                native_enum=False,
                create_constraint=False,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "decision_provenance",
            sa.Enum(
                "automatic_approved",
                "manual_approved",
                "manual_rejected",
                "manual_review",
                name="compensation_decision_provenance",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=True,
        ),
        sa.Column("approved_refund_amount", sa.BigInteger(), nullable=True),
        sa.Column("decision_reason_code", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        *constraints,
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_compensation_cases_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["payment_transactions.id"],
            name=op.f("fk_compensation_cases_transaction_id_payment_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_attempt_id"],
            ["payment_attempts.id"],
            name=op.f("fk_compensation_cases_payment_attempt_id_payment_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["entitlement_id"],
            ["entitlements.id"],
            name=op.f("fk_compensation_cases_entitlement_id_entitlements"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["fulfillment_execution_id"],
            ["fulfillment_executions.id"],
            name=op.f("fk_compensation_cases_fulfillment_execution_id_fulfillment_executions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"],
            ["quotes.id"],
            name=op.f("fk_compensation_cases_quote_id_quotes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_compensation_cases_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_compensation_cases_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compensation_cases")),
        sa.UniqueConstraint(
            "fulfillment_execution_id",
            name=op.f("uq_compensation_cases_fulfillment_execution_id"),
        ),
    )
    op.create_index(
        "ix_compensation_cases_account_created_at",
        "compensation_cases",
        ["account_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_compensation_cases_transaction",
        "compensation_cases",
        ["transaction_id"],
        unique=False,
    )
    op.create_index(
        "ix_compensation_cases_state_created_at",
        "compensation_cases",
        ["decision_state", "created_at"],
        unique=False,
    )


def _create_payment_refunds() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'rfd_'",
            name=op.f("ck_payment_refunds_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(compensation_case_id) = 30 AND substr(compensation_case_id, 1, 4) = 'cmp_'",
            name=op.f("ck_payment_refunds_compensation_case_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name=op.f("ck_payment_refunds_transaction_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(payment_attempt_id) = 30 AND substr(payment_attempt_id, 1, 4) = 'pmt_'",
            name=op.f("ck_payment_refunds_payment_attempt_id_prefix"),
        ),
        sa.CheckConstraint(
            "provider = 'razorpay'",
            name=op.f("ck_payment_refunds_provider"),
        ),
        sa.CheckConstraint(
            "length(provider_payment_id) BETWEEN 1 AND 64 "
            "AND provider_payment_id = trim(provider_payment_id)",
            name=op.f("ck_payment_refunds_provider_payment_id_valid"),
        ),
        sa.CheckConstraint(
            "provider_refund_id IS NULL OR "
            "(length(provider_refund_id) BETWEEN 1 AND 64 "
            "AND provider_refund_id = trim(provider_refund_id))",
            name=op.f("ck_payment_refunds_provider_refund_id_valid"),
        ),
        sa.CheckConstraint(
            "amount BETWEEN 1 AND 9007199254740991",
            name=op.f("ck_payment_refunds_amount_safe_integer"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_payment_refunds_currency_uppercase"),
        ),
        sa.CheckConstraint(
            "refund_state IN ('refund_pending', 'refund_processing', 'refunded', "
            "'refund_failed', 'refund_uncertain', 'reconciliation_required')",
            name=op.f("ck_payment_refunds_refund_state"),
        ),
        sa.CheckConstraint(
            "provider_status IS NULL OR provider_status IN ('pending', 'processed', 'failed')",
            name=op.f("ck_payment_refunds_provider_status"),
        ),
        sa.CheckConstraint(
            "(refund_state <> 'refunded' OR provider_status = 'processed') AND "
            "(refund_state <> 'refund_failed' OR "
            "((provider_refund_id IS NULL AND provider_status IS NULL) OR "
            "(provider_refund_id IS NOT NULL AND provider_status = 'failed')))",
            name=op.f("ck_payment_refunds_terminal_provider_evidence"),
        ),
        sa.CheckConstraint(
            "length(provider_receipt) BETWEEN 1 AND 40 "
            "AND provider_receipt = trim(provider_receipt)",
            name=op.f("ck_payment_refunds_provider_receipt_valid"),
        ),
        sa.CheckConstraint(
            "provider_receipt = id",
            name=op.f("ck_payment_refunds_provider_receipt_is_id"),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_payment_refunds_revision_positive"),
        ),
        sa.CheckConstraint(
            "requested_at IS NULL OR requested_at >= created_at",
            name=op.f("ck_payment_refunds_requested_after_creation"),
        ),
        sa.CheckConstraint(
            "processed_at IS NULL OR (requested_at IS NOT NULL AND processed_at >= requested_at)",
            name=op.f("ck_payment_refunds_processed_after_request"),
        ),
        sa.CheckConstraint(
            "failed_at IS NULL OR (requested_at IS NOT NULL AND failed_at >= requested_at)",
            name=op.f("ck_payment_refunds_failed_after_request"),
        ),
        sa.CheckConstraint(
            "last_reconciled_at IS NULL OR "
            "(requested_at IS NOT NULL AND last_reconciled_at >= requested_at)",
            name=op.f("ck_payment_refunds_reconciled_after_request"),
        ),
        sa.CheckConstraint(
            "((reconciliation_required_at IS NULL AND reconciliation_reason_code IS NULL) OR "
            "(reconciliation_required_at IS NOT NULL "
            "AND reconciliation_reason_code IS NOT NULL))",
            name=op.f("ck_payment_refunds_reconciliation_overlay_pair"),
        ),
        sa.CheckConstraint(
            "reconciliation_required_at IS NULL OR "
            "(requested_at IS NOT NULL AND reconciliation_required_at >= requested_at)",
            name=op.f("ck_payment_refunds_reconciliation_required_after_request"),
        ),
        sa.CheckConstraint(
            "reconciliation_reason_code IS NULL OR "
            "(length(reconciliation_reason_code) BETWEEN 1 AND 100 "
            "AND reconciliation_reason_code = trim(reconciliation_reason_code) "
            "AND reconciliation_reason_code = upper(reconciliation_reason_code))",
            name=op.f("ck_payment_refunds_reconciliation_reason_code_valid"),
        ),
        sa.CheckConstraint(
            "((refund_state = 'refund_pending' AND provider_refund_id IS NULL "
            "AND provider_status IS NULL AND requested_at IS NULL "
            "AND processed_at IS NULL AND failed_at IS NULL) OR "
            "(refund_state IN "
            "('refund_processing', 'refund_uncertain', 'reconciliation_required') "
            "AND requested_at IS NOT NULL AND processed_at IS NULL AND failed_at IS NULL) OR "
            "(refund_state = 'refunded' AND provider_refund_id IS NOT NULL "
            "AND provider_status IS NOT NULL AND requested_at IS NOT NULL "
            "AND processed_at IS NOT NULL AND failed_at IS NULL) OR "
            "(refund_state = 'refund_failed' AND requested_at IS NOT NULL "
            "AND processed_at IS NULL AND failed_at IS NOT NULL))",
            name=op.f("ck_payment_refunds_state_payload"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^rfd_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_payment_refunds_id_format"),
                ),
                sa.CheckConstraint(
                    "provider_payment_id ~ '^pay_[A-Za-z0-9]+$'",
                    name=op.f("ck_payment_refunds_provider_payment_id_format"),
                ),
                sa.CheckConstraint(
                    "currency ~ '^[A-Z]{3}$'",
                    name=op.f("ck_payment_refunds_currency_format"),
                ),
                sa.CheckConstraint(
                    "reconciliation_reason_code IS NULL OR "
                    "reconciliation_reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
                    name=op.f("ck_payment_refunds_reconciliation_reason_code_format"),
                ),
            )
        )
    op.create_table(
        "payment_refunds",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("compensation_case_id", sa.String(length=30), nullable=False),
        sa.Column("transaction_id", sa.String(length=30), nullable=False),
        sa.Column("payment_attempt_id", sa.String(length=30), nullable=False),
        sa.Column(
            "provider",
            sa.Enum(
                "razorpay",
                name="payment_provider",
                native_enum=False,
                create_constraint=False,
            ),
            server_default="razorpay",
            nullable=False,
        ),
        sa.Column("provider_payment_id", sa.String(length=64), nullable=False),
        sa.Column("provider_refund_id", sa.String(length=64), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "refund_state",
            sa.Enum(
                "refund_pending",
                "refund_processing",
                "refunded",
                "refund_failed",
                "refund_uncertain",
                "reconciliation_required",
                name="payment_refund_state",
                native_enum=False,
                create_constraint=False,
            ),
            server_default="refund_pending",
            nullable=False,
        ),
        sa.Column("provider_status", sa.String(length=64), nullable=True),
        sa.Column("provider_receipt", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_required_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_reason_code", sa.String(length=100), nullable=True),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        *constraints,
        sa.ForeignKeyConstraint(
            ["compensation_case_id"],
            ["compensation_cases.id"],
            name=op.f("fk_payment_refunds_compensation_case_id_compensation_cases"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["payment_transactions.id"],
            name=op.f("fk_payment_refunds_transaction_id_payment_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_attempt_id"],
            ["payment_attempts.id"],
            name=op.f("fk_payment_refunds_payment_attempt_id_payment_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_payment_id"],
            ["payment_attempts.provider_payment_id"],
            name=op.f("fk_payment_refunds_provider_payment_id_payment_attempts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_refunds")),
        sa.UniqueConstraint(
            "provider_refund_id",
            name=op.f("uq_payment_refunds_provider_refund_id"),
        ),
        sa.UniqueConstraint(
            "provider_receipt",
            name=op.f("uq_payment_refunds_provider_receipt"),
        ),
    )
    op.create_index(
        "ix_payment_refunds_transaction_created_at",
        "payment_refunds",
        ["transaction_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_refunds_case_created_at",
        "payment_refunds",
        ["compensation_case_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_payment_refunds_state_created_at",
        "payment_refunds",
        ["refund_state", "created_at"],
        unique=False,
    )
    active_states = sa.text(
        "refund_state IN ('refund_pending', 'refund_processing', "
        "'refund_uncertain', 'reconciliation_required')"
    )
    op.create_index(
        "uq_payment_refunds_one_active_per_case",
        "payment_refunds",
        ["compensation_case_id"],
        unique=True,
        postgresql_where=active_states,
        sqlite_where=active_states,
    )


def _create_compensation_events() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'cpe_'",
            name=op.f("ck_compensation_events_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(compensation_case_id) = 30 AND substr(compensation_case_id, 1, 4) = 'cmp_'",
            name=op.f("ck_compensation_events_compensation_case_id_prefix"),
        ),
        sa.CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name=op.f("ck_compensation_events_transaction_id_prefix"),
        ),
        sa.CheckConstraint(
            "payment_refund_id IS NULL OR "
            "(length(payment_refund_id) = 30 AND substr(payment_refund_id, 1, 4) = 'rfd_')",
            name=op.f("ck_compensation_events_payment_refund_id_prefix"),
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name=op.f("ck_compensation_events_sequence_positive"),
        ),
        sa.CheckConstraint(
            "case_revision >= 1",
            name=op.f("ck_compensation_events_case_revision_positive"),
        ),
        sa.CheckConstraint(
            "refund_revision IS NULL OR refund_revision >= 1",
            name=op.f("ck_compensation_events_refund_revision_positive"),
        ),
        sa.CheckConstraint(
            "event_type IN ('compensation_case_created', 'compensation_recommended', "
            "'compensation_approved', 'compensation_rejected', 'refund_outbox_created', "
            "'refund_requested', 'refund_request_started', 'razorpay_refund_created', "
            "'refund_processing', 'refund_completed', 'refund_failed', 'refund_uncertain', "
            "'refund_reconciliation_required', 'refund_reconciliation_resolved', "
            "'compensation_closed')",
            name=op.f("ck_compensation_events_event_type"),
        ),
        sa.CheckConstraint(
            "actor_type IN "
            "('system', 'refund_worker', 'provider_api', 'provider_webhook', 'operator')",
            name=op.f("ck_compensation_events_actor_type"),
        ),
        sa.CheckConstraint(
            "actor_id IS NULL OR "
            "(length(actor_id) BETWEEN 1 AND 128 AND actor_id = trim(actor_id))",
            name=op.f("ck_compensation_events_actor_id_valid"),
        ),
        sa.CheckConstraint(
            "((event_type IN ('compensation_case_created', 'compensation_recommended', "
            "'compensation_approved', 'compensation_rejected', 'refund_outbox_created') "
            "AND payment_refund_id IS NULL AND refund_revision IS NULL) OR "
            "(event_type IN ('refund_requested', 'refund_request_started', "
            "'razorpay_refund_created', 'refund_processing', 'refund_completed', "
            "'refund_failed', 'refund_uncertain', 'refund_reconciliation_required', "
            "'refund_reconciliation_resolved') "
            "AND payment_refund_id IS NOT NULL AND refund_revision IS NOT NULL) OR "
            "(event_type = 'compensation_closed' AND "
            "((payment_refund_id IS NULL AND refund_revision IS NULL) OR "
            "(payment_refund_id IS NOT NULL AND refund_revision IS NOT NULL))))",
            name=op.f("ck_compensation_events_event_binding"),
        ),
        sa.CheckConstraint(
            "length(reason_code) BETWEEN 1 AND 100 AND reason_code = trim(reason_code)",
            name=op.f("ck_compensation_events_reason_code_valid"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 AND idempotency_key = trim(idempotency_key)",
            name=op.f("ck_compensation_events_idempotency_key_valid"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^cpe_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_compensation_events_id_format"),
                ),
                sa.CheckConstraint(
                    "reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
                    name=op.f("ck_compensation_events_reason_code_format"),
                ),
                sa.CheckConstraint(
                    "jsonb_typeof(metadata) = 'object'",
                    name=op.f("ck_compensation_events_metadata_object"),
                ),
            )
        )
    op.create_table(
        "compensation_events",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("compensation_case_id", sa.String(length=30), nullable=False),
        sa.Column("transaction_id", sa.String(length=30), nullable=False),
        sa.Column("payment_refund_id", sa.String(length=30), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("case_revision", sa.BigInteger(), nullable=False),
        sa.Column("refund_revision", sa.BigInteger(), nullable=True),
        sa.Column(
            "event_type",
            sa.Enum(
                "compensation_case_created",
                "compensation_recommended",
                "compensation_approved",
                "compensation_rejected",
                "refund_outbox_created",
                "refund_requested",
                "refund_request_started",
                "razorpay_refund_created",
                "refund_processing",
                "refund_completed",
                "refund_failed",
                "refund_uncertain",
                "refund_reconciliation_required",
                "refund_reconciliation_resolved",
                "compensation_closed",
                name="compensation_event_type",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "actor_type",
            sa.Enum(
                "system",
                "refund_worker",
                "provider_api",
                "provider_webhook",
                "operator",
                name="compensation_event_actor_type",
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
            ["compensation_case_id"],
            ["compensation_cases.id"],
            name=op.f("fk_compensation_events_compensation_case_id_compensation_cases"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["payment_transactions.id"],
            name=op.f("fk_compensation_events_transaction_id_payment_transactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_refund_id"],
            ["payment_refunds.id"],
            name=op.f("fk_compensation_events_payment_refund_id_payment_refunds"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compensation_events")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_compensation_events_idempotency_key"),
        ),
        sa.UniqueConstraint(
            "compensation_case_id",
            "sequence",
            name=op.f("uq_compensation_events_case_sequence"),
        ),
    )
    op.create_index(
        "ix_compensation_events_transaction_occurred",
        "compensation_events",
        ["transaction_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_compensation_events_case_sequence",
        "compensation_events",
        ["compensation_case_id", "sequence"],
        unique=False,
    )
    op.create_index(
        "ix_compensation_events_refund_occurred",
        "compensation_events",
        ["payment_refund_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_compensation_events_type_occurred",
        "compensation_events",
        ["event_type", "occurred_at"],
        unique=False,
    )


def _create_refund_outbox_events() -> None:
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'rox_'",
            name=op.f("ck_refund_outbox_events_id_prefix"),
        ),
        sa.CheckConstraint(
            "event_type = 'refund_requested'",
            name=op.f("ck_refund_outbox_events_event_type"),
        ),
        sa.CheckConstraint(
            "length(compensation_case_id) = 30 AND substr(compensation_case_id, 1, 4) = 'cmp_'",
            name=op.f("ck_refund_outbox_events_compensation_case_id_prefix"),
        ),
        sa.CheckConstraint(
            "deduplication_key = 'refund:' || compensation_case_id",
            name=op.f("ck_refund_outbox_events_deduplication_binding"),
        ),
        sa.CheckConstraint(
            "payload_version = '1'",
            name=op.f("ck_refund_outbox_events_payload_version"),
        ),
        sa.CheckConstraint(
            "available_at >= created_at",
            name=op.f("ck_refund_outbox_events_availability"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_refund_outbox_events_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "lease_generation >= 0",
            name=op.f("ck_refund_outbox_events_lease_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "(processing_started_at IS NULL AND lease_expires_at IS NULL) OR "
            "(processing_started_at IS NOT NULL AND lease_expires_at > processing_started_at)",
            name=op.f("ck_refund_outbox_events_lease_complete"),
        ),
        sa.CheckConstraint(
            "processed_at IS NULL OR processing_started_at IS NOT NULL",
            name=op.f("ck_refund_outbox_events_processed_claimed"),
        ),
        sa.CheckConstraint(
            "processed_at IS NULL OR processed_at >= processing_started_at",
            name=op.f("ck_refund_outbox_events_processed_after_claim"),
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR "
            "(length(last_error_code) BETWEEN 1 AND 100 "
            "AND last_error_code = trim(last_error_code))",
            name=op.f("ck_refund_outbox_events_last_error_code_valid"),
        ),
    ]
    if op.get_context().dialect.name == "postgresql":
        constraints.extend(
            (
                sa.CheckConstraint(
                    "id ~ '^rox_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
                    name=op.f("ck_refund_outbox_events_id_format"),
                ),
                sa.CheckConstraint(
                    "jsonb_typeof(payload) = 'object'",
                    name=op.f("ck_refund_outbox_events_payload_object"),
                ),
                sa.CheckConstraint(
                    "last_error_code IS NULL OR last_error_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
                    name=op.f("ck_refund_outbox_events_last_error_code_format"),
                ),
            )
        )
    op.create_table(
        "refund_outbox_events",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "refund_requested",
                name="refund_outbox_event_type",
                native_enum=False,
                create_constraint=False,
            ),
            server_default="refund_requested",
            nullable=False,
        ),
        sa.Column("compensation_case_id", sa.String(length=30), nullable=False),
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
            ["compensation_case_id"],
            ["compensation_cases.id"],
            name=op.f("fk_refund_outbox_events_compensation_case_id_compensation_cases"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refund_outbox_events")),
        sa.UniqueConstraint(
            "compensation_case_id",
            name=op.f("uq_refund_outbox_events_case_id"),
        ),
        sa.UniqueConstraint(
            "deduplication_key",
            name=op.f("uq_refund_outbox_events_dedup_key"),
        ),
    )
    op.create_index(
        "ix_refund_outbox_events_due",
        "refund_outbox_events",
        ["available_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL"),
        sqlite_where=sa.text("processed_at IS NULL"),
    )
    op.create_index(
        "ix_refund_outbox_events_stale_lease",
        "refund_outbox_events",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL AND lease_expires_at IS NOT NULL"),
        sqlite_where=sa.text("processed_at IS NULL AND lease_expires_at IS NOT NULL"),
    )


def _create_postgresql_guards() -> None:
    if op.get_context().dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE FUNCTION metergate_validate_compensation_case_evidence()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM payment_transactions payment_txn
                JOIN payment_attempts attempt
                  ON attempt.id = NEW.payment_attempt_id
                 AND attempt.transaction_id = payment_txn.id
                JOIN entitlements entitlement
                  ON entitlement.id = NEW.entitlement_id
                 AND entitlement.transaction_id = payment_txn.id
                JOIN fulfillment_executions execution
                  ON execution.id = NEW.fulfillment_execution_id
                 AND execution.entitlement_id = entitlement.id
                 AND execution.transaction_id = payment_txn.id
                JOIN quotes quote
                  ON quote.id = NEW.quote_id
                WHERE payment_txn.id = NEW.transaction_id
                  AND payment_txn.transaction_state = 'paid'
                  AND payment_txn.account_id = NEW.account_id
                  AND payment_txn.quote_id = quote.id
                  AND payment_txn.quote_hash = NEW.quote_hash
                  AND payment_txn.merchant_id = NEW.merchant_id
                  AND payment_txn.service_id = NEW.service_id
                  AND payment_txn.amount = NEW.amount_paid
                  AND payment_txn.currency = NEW.currency
                  AND attempt.captured
                  AND attempt.amount = payment_txn.amount
                  AND attempt.currency = payment_txn.currency
                  AND entitlement.account_id = payment_txn.account_id
                  AND entitlement.provider_payment_id = attempt.provider_payment_id
                  AND entitlement.quote_id = quote.id
                  AND entitlement.quote_hash = NEW.quote_hash
                  AND entitlement.merchant_id = payment_txn.merchant_id
                  AND entitlement.service_id = payment_txn.service_id
                  AND entitlement.amount = payment_txn.amount
                  AND entitlement.currency = payment_txn.currency
                  AND execution.account_id = payment_txn.account_id
                  AND execution.merchant_id = payment_txn.merchant_id
                  AND execution.service_id = payment_txn.service_id
                  AND execution.input_hash = entitlement.input_hash
                  AND execution.execution_state = 'permanent_failure'
                  AND execution.compensation_required
                  AND execution.failure_code = NEW.failure_code
                  AND execution.result_content_type IS NULL
                  AND execution.result_json IS NULL
                  AND execution.result_hash IS NULL
                  AND execution.result_size_bytes IS NULL
                  AND quote.quote_hash = NEW.quote_hash
                  AND quote.merchant_id = payment_txn.merchant_id
                  AND quote.service_id = payment_txn.service_id
                  AND quote.amount = payment_txn.amount
                  AND quote.currency = payment_txn.currency
                  AND (NEW.decision_provenance IS DISTINCT FROM 'automatic_approved'
                       OR quote.refund_on_fulfillment_failure)
            ) THEN
                RAISE EXCEPTION
                    'compensation case % lacks exact paid terminal-failure evidence', NEW.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'compensation_case_requires_terminal_paid_evidence';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_compensation_cases_validate_evidence
        AFTER INSERT OR UPDATE ON compensation_cases
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION metergate_validate_compensation_case_evidence()
        """
    )

    op.execute(
        """
        CREATE FUNCTION metergate_guard_compensation_case()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'compensation cases cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF ROW(NEW.id, NEW.account_id, NEW.transaction_id, NEW.payment_attempt_id,
                   NEW.entitlement_id, NEW.fulfillment_execution_id, NEW.quote_id,
                   NEW.quote_hash, NEW.merchant_id, NEW.service_id, NEW.amount_paid,
                   NEW.currency, NEW.failure_code, NEW.failure_evidence_version,
                   NEW.failure_evidence_hash, NEW.recommended_action, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.account_id, OLD.transaction_id, OLD.payment_attempt_id,
                   OLD.entitlement_id, OLD.fulfillment_execution_id, OLD.quote_id,
                   OLD.quote_hash, OLD.merchant_id, OLD.service_id, OLD.amount_paid,
                   OLD.currency, OLD.failure_code, OLD.failure_evidence_version,
                   OLD.failure_evidence_hash, OLD.recommended_action, OLD.created_at) THEN
                RAISE EXCEPTION 'compensation case evidence and bindings are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'compensation case revision must advance exactly once'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.decision_state IN ('rejected', 'completed') AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'terminal compensation case evidence is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.decision_state <> OLD.decision_state AND NOT (
                (OLD.decision_state = 'pending' AND NEW.decision_state IN
                    ('approved', 'rejected', 'manual_review')) OR
                (OLD.decision_state = 'manual_review' AND NEW.decision_state IN
                    ('approved', 'rejected')) OR
                (OLD.decision_state = 'approved' AND NEW.decision_state = 'executing') OR
                (OLD.decision_state = 'executing' AND NEW.decision_state IN
                    ('completed', 'manual_review'))
            ) THEN
                RAISE EXCEPTION 'compensation case state transition is not allowed'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.decision_state NOT IN ('pending', 'manual_review') AND
               (NEW.decision_provenance IS DISTINCT FROM OLD.decision_provenance OR
                NEW.decision_reason_code IS DISTINCT FROM OLD.decision_reason_code) THEN
                RAISE EXCEPTION 'compensation decision provenance and reason are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.approved_refund_amount IS NOT NULL
               AND NEW.approved_refund_amount IS DISTINCT FROM OLD.approved_refund_amount THEN
                RAISE EXCEPTION 'approved compensation amount is immutable once set'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.decided_at IS NOT NULL AND NEW.decided_at IS DISTINCT FROM OLD.decided_at THEN
                RAISE EXCEPTION 'compensation decision time is immutable once recorded'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.closed_at IS NOT NULL AND NEW.closed_at IS DISTINCT FROM OLD.closed_at THEN
                RAISE EXCEPTION 'compensation closure time is immutable once recorded'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_compensation_cases_guard
        BEFORE UPDATE OR DELETE ON compensation_cases
        FOR EACH ROW EXECUTE FUNCTION metergate_guard_compensation_case()
        """
    )

    op.execute(
        """
        CREATE FUNCTION metergate_guard_payment_refund()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'payment refunds cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF ROW(NEW.id, NEW.compensation_case_id, NEW.transaction_id,
                   NEW.payment_attempt_id, NEW.provider, NEW.provider_payment_id,
                   NEW.amount, NEW.currency, NEW.provider_receipt, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.compensation_case_id, OLD.transaction_id,
                   OLD.payment_attempt_id, OLD.provider, OLD.provider_payment_id,
                   OLD.amount, OLD.currency, OLD.provider_receipt, OLD.created_at) THEN
                RAISE EXCEPTION 'payment refund binding and amount are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'payment refund revision must advance exactly once'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.refund_state IN ('refunded', 'refund_failed') AND
               ROW(NEW.id, NEW.compensation_case_id, NEW.transaction_id,
                   NEW.payment_attempt_id, NEW.provider, NEW.provider_payment_id,
                   NEW.provider_refund_id, NEW.amount, NEW.currency, NEW.refund_state,
                   NEW.provider_status, NEW.provider_receipt, NEW.created_at,
                   NEW.requested_at, NEW.processed_at, NEW.failed_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.compensation_case_id, OLD.transaction_id,
                   OLD.payment_attempt_id, OLD.provider, OLD.provider_payment_id,
                   OLD.provider_refund_id, OLD.amount, OLD.currency, OLD.refund_state,
                   OLD.provider_status, OLD.provider_receipt, OLD.created_at,
                   OLD.requested_at, OLD.processed_at, OLD.failed_at) THEN
                RAISE EXCEPTION 'terminal payment refund evidence is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.refund_state <> OLD.refund_state AND NOT (
                (OLD.refund_state = 'refund_pending' AND NEW.refund_state IN
                    ('refund_processing', 'refund_failed', 'refund_uncertain',
                     'reconciliation_required')) OR
                (OLD.refund_state = 'refund_processing' AND NEW.refund_state IN
                    ('refunded', 'refund_failed', 'refund_uncertain',
                     'reconciliation_required')) OR
                (OLD.refund_state = 'refund_uncertain' AND NEW.refund_state IN
                    ('refund_processing', 'refunded', 'refund_failed',
                     'reconciliation_required')) OR
                (OLD.refund_state = 'reconciliation_required' AND NEW.refund_state IN
                    ('refund_processing', 'refunded', 'refund_failed'))
            ) THEN
                RAISE EXCEPTION 'payment refund state transition is not allowed'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.provider_refund_id IS NOT NULL
               AND NEW.provider_refund_id IS DISTINCT FROM OLD.provider_refund_id THEN
                RAISE EXCEPTION 'provider refund binding is immutable once set'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.requested_at IS NOT NULL
               AND NEW.requested_at IS DISTINCT FROM OLD.requested_at THEN
                RAISE EXCEPTION 'refund request time is immutable once recorded'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.processed_at IS NOT NULL
               AND NEW.processed_at IS DISTINCT FROM OLD.processed_at THEN
                RAISE EXCEPTION 'refund processed time is immutable once recorded'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.failed_at IS NOT NULL AND NEW.failed_at IS DISTINCT FROM OLD.failed_at THEN
                RAISE EXCEPTION 'refund failure time is immutable once recorded'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.last_reconciled_at IS NOT NULL AND
               (NEW.last_reconciled_at IS NULL OR
                NEW.last_reconciled_at < OLD.last_reconciled_at) THEN
                RAISE EXCEPTION 'refund reconciliation time cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.reconciliation_required_at IS NOT NULL AND
               NEW.reconciliation_required_at IS NOT NULL AND
               NEW.reconciliation_required_at < OLD.reconciliation_required_at THEN
                RAISE EXCEPTION 'refund reconciliation overlay time cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payment_refunds_guard
        BEFORE UPDATE OR DELETE ON payment_refunds
        FOR EACH ROW EXECUTE FUNCTION metergate_guard_payment_refund()
        """
    )

    op.execute(
        """
        CREATE FUNCTION metergate_guard_refund_reservation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            case_amount bigint;
            case_approved_amount bigint;
            case_currency text;
            case_transaction_id text;
            case_attempt_id text;
            case_state text;
            case_provenance text;
            captured_amount bigint;
            captured_currency text;
            captured_transaction_id text;
            captured_payment_id text;
            is_captured boolean;
            case_reserved bigint;
            payment_reserved bigint;
            next_amount bigint;
        BEGIN
            SELECT compensation.amount_paid, compensation.approved_refund_amount,
                   compensation.currency, compensation.transaction_id,
                   compensation.payment_attempt_id, compensation.decision_state,
                   compensation.decision_provenance, attempt.amount, attempt.currency,
                   attempt.transaction_id, attempt.provider_payment_id, attempt.captured
              INTO case_amount, case_approved_amount, case_currency, case_transaction_id,
                   case_attempt_id, case_state, case_provenance, captured_amount,
                   captured_currency, captured_transaction_id, captured_payment_id,
                   is_captured
              FROM compensation_cases compensation
              JOIN payment_attempts attempt ON attempt.id = compensation.payment_attempt_id
             WHERE compensation.id = NEW.compensation_case_id
             FOR UPDATE OF compensation, attempt;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'refund % lacks a compensation case/payment attempt', NEW.id
                    USING ERRCODE = '23503';
            END IF;
            IF NEW.transaction_id <> case_transaction_id
               OR NEW.payment_attempt_id <> case_attempt_id
               OR NEW.provider_payment_id <> captured_payment_id
               OR captured_transaction_id <> case_transaction_id
               OR NEW.currency <> case_currency
               OR captured_currency <> case_currency
               OR NOT is_captured THEN
                RAISE EXCEPTION 'refund % does not bind exact captured payment evidence', NEW.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'refund_requires_exact_captured_payment';
            END IF;
            IF TG_OP = 'INSERT' AND case_state NOT IN ('approved', 'executing') THEN
                RAISE EXCEPTION 'new refund % requires an approved compensation case', NEW.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'refund_requires_approved_compensation';
            END IF;
            IF TG_OP = 'UPDATE' AND NOT (
                case_state IN ('approved', 'executing', 'completed') OR
                (case_state = 'manual_review'
                 AND case_provenance IN ('automatic_approved', 'manual_approved')
                 AND case_approved_amount IS NOT NULL)
            ) THEN
                RAISE EXCEPTION 'refund % cannot advance under current compensation state', NEW.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'refund_requires_approved_compensation';
            END IF;
            IF case_approved_amount IS NULL OR NEW.amount > case_amount
               OR NEW.amount > captured_amount THEN
                RAISE EXCEPTION 'refund % amount exceeds approved/captured value', NEW.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'refund_amount_within_approved_capture';
            END IF;

            SELECT COALESCE(sum(refund.amount), 0)::bigint
              INTO case_reserved
              FROM payment_refunds refund
             WHERE refund.compensation_case_id = NEW.compensation_case_id
               AND refund.id <> NEW.id
               AND refund.refund_state <> 'refund_failed';
            SELECT COALESCE(sum(refund.amount), 0)::bigint
              INTO payment_reserved
              FROM payment_refunds refund
             WHERE refund.payment_attempt_id = NEW.payment_attempt_id
               AND refund.id <> NEW.id
               AND refund.refund_state <> 'refund_failed';

            next_amount := CASE WHEN NEW.refund_state = 'refund_failed' THEN 0 ELSE NEW.amount END;
            IF case_reserved + next_amount > case_approved_amount THEN
                RAISE EXCEPTION 'refund reservations exceed approved compensation amount'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'refund_case_reservation_not_overrun';
            END IF;
            IF payment_reserved + next_amount > captured_amount THEN
                RAISE EXCEPTION 'refund reservations exceed captured payment amount'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'refund_payment_reservation_not_overrun';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payment_refunds_reservation_guard
        BEFORE INSERT OR UPDATE ON payment_refunds
        FOR EACH ROW EXECUTE FUNCTION metergate_guard_refund_reservation()
        """
    )

    op.execute(
        """
        CREATE FUNCTION metergate_reject_compensation_event_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'compensation events are immutable; % is not allowed', TG_OP
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_compensation_events_immutable
        BEFORE UPDATE OR DELETE ON compensation_events
        FOR EACH ROW EXECUTE FUNCTION metergate_reject_compensation_event_mutation()
        """
    )

    op.execute(
        """
        CREATE FUNCTION metergate_guard_refund_outbox_event()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'refund outbox events cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF OLD.processed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'processed refund outbox events are terminal'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(NEW.id, NEW.event_type, NEW.compensation_case_id,
                   NEW.deduplication_key, NEW.payload_version, NEW.payload, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.event_type, OLD.compensation_case_id,
                   OLD.deduplication_key, OLD.payload_version, OLD.payload, OLD.created_at) THEN
                RAISE EXCEPTION 'refund outbox binding and payload are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.attempt_count < OLD.attempt_count
               OR NEW.lease_generation < OLD.lease_generation THEN
                RAISE EXCEPTION 'refund outbox counters cannot regress' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_refund_outbox_events_guard
        BEFORE UPDATE OR DELETE ON refund_outbox_events
        FOR EACH ROW EXECUTE FUNCTION metergate_guard_refund_outbox_event()
        """
    )

    op.execute(
        """
        CREATE FUNCTION metergate_validate_compensation_event_binding()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM compensation_cases compensation
                 WHERE compensation.id = NEW.compensation_case_id
                   AND compensation.transaction_id = NEW.transaction_id
                   AND compensation.revision >= NEW.case_revision
            ) THEN
                RAISE EXCEPTION 'compensation event does not bind its case revision'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'compensation_event_case_binding';
            END IF;
            IF NEW.payment_refund_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM payment_refunds refund
                 WHERE refund.id = NEW.payment_refund_id
                   AND refund.compensation_case_id = NEW.compensation_case_id
                   AND refund.transaction_id = NEW.transaction_id
                   AND refund.revision >= NEW.refund_revision
            ) THEN
                RAISE EXCEPTION 'compensation event does not bind its refund revision'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'compensation_event_refund_binding';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_compensation_events_validate_binding
        AFTER INSERT ON compensation_events
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION metergate_validate_compensation_event_binding()
        """
    )

    op.execute(
        """
        CREATE FUNCTION metergate_require_compensation_case_event()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM compensation_events event
                 WHERE event.compensation_case_id = NEW.id
                   AND event.transaction_id = NEW.transaction_id
                   AND event.case_revision = NEW.revision
            ) THEN
                RAISE EXCEPTION 'compensation case revision % requires audit evidence', NEW.revision
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'compensation_case_revision_requires_event';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_compensation_cases_require_event
        AFTER INSERT OR UPDATE ON compensation_cases
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION metergate_require_compensation_case_event()
        """
    )

    op.execute(
        """
        CREATE FUNCTION metergate_require_payment_refund_event()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM compensation_events event
                 WHERE event.payment_refund_id = NEW.id
                   AND event.compensation_case_id = NEW.compensation_case_id
                   AND event.transaction_id = NEW.transaction_id
                   AND event.refund_revision = NEW.revision
            ) THEN
                RAISE EXCEPTION 'payment refund revision % requires audit evidence', NEW.revision
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'payment_refund_revision_requires_event';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_payment_refunds_require_event
        AFTER INSERT OR UPDATE ON payment_refunds
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION metergate_require_payment_refund_event()
        """
    )

    op.execute(
        """
        CREATE FUNCTION metergate_require_completed_refund_total()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            processed_refund_total bigint;
        BEGIN
            IF NEW.decision_state <> 'completed' OR
               (TG_OP = 'UPDATE' AND OLD.decision_state = 'completed') THEN
                RETURN NULL;
            END IF;

            SELECT COALESCE(sum(refund.amount), 0)::bigint
              INTO processed_refund_total
              FROM payment_refunds refund
             WHERE refund.compensation_case_id = NEW.id
               AND refund.refund_state = 'refunded'
               AND refund.provider_status = 'processed';

            IF NEW.approved_refund_amount IS NULL OR
               processed_refund_total <> NEW.approved_refund_amount THEN
                RAISE EXCEPTION
                    'completed compensation case % lacks its exact processed refund total', NEW.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'completed_compensation_requires_exact_processed_refund';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_compensation_cases_completed_refund_total
        AFTER INSERT OR UPDATE OF decision_state ON compensation_cases
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION metergate_require_completed_refund_total()
        """
    )

    op.execute(
        """
        CREATE FUNCTION metergate_require_approved_refund_outbox()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.decision_state = 'approved'
               AND (TG_OP = 'INSERT' OR OLD.decision_state IS DISTINCT FROM 'approved')
               AND NOT EXISTS (
                   SELECT 1 FROM refund_outbox_events event
                    WHERE event.compensation_case_id = NEW.id
                      AND event.event_type = 'refund_requested'
                      AND event.deduplication_key = 'refund:' || NEW.id
               ) THEN
                RAISE EXCEPTION 'approved compensation case % requires refund outbox work', NEW.id
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'approved_compensation_requires_refund_outbox';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_compensation_cases_approved_refund_outbox
        AFTER INSERT OR UPDATE OF decision_state ON compensation_cases
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION metergate_require_approved_refund_outbox()
        """
    )


def upgrade() -> None:
    _create_compensation_cases()
    _create_payment_refunds()
    _create_compensation_events()
    _create_refund_outbox_events()
    _create_postgresql_guards()


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_compensation_cases_approved_refund_outbox "
            "ON compensation_cases"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_require_approved_refund_outbox()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_compensation_cases_completed_refund_total "
            "ON compensation_cases"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_require_completed_refund_total()")
        op.execute("DROP TRIGGER IF EXISTS trg_payment_refunds_require_event ON payment_refunds")
        op.execute("DROP FUNCTION IF EXISTS metergate_require_payment_refund_event()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_compensation_cases_require_event ON compensation_cases"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_require_compensation_case_event()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_compensation_events_validate_binding ON compensation_events"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_validate_compensation_event_binding()")
        op.execute("DROP TRIGGER IF EXISTS trg_refund_outbox_events_guard ON refund_outbox_events")
        op.execute("DROP FUNCTION IF EXISTS metergate_guard_refund_outbox_event()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_compensation_events_immutable ON compensation_events"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_reject_compensation_event_mutation()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_payment_refunds_reservation_guard ON payment_refunds"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_guard_refund_reservation()")
        op.execute("DROP TRIGGER IF EXISTS trg_payment_refunds_guard ON payment_refunds")
        op.execute("DROP FUNCTION IF EXISTS metergate_guard_payment_refund()")
        op.execute("DROP TRIGGER IF EXISTS trg_compensation_cases_guard ON compensation_cases")
        op.execute("DROP FUNCTION IF EXISTS metergate_guard_compensation_case()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_compensation_cases_validate_evidence ON compensation_cases"
        )
        op.execute("DROP FUNCTION IF EXISTS metergate_validate_compensation_case_evidence()")

    op.drop_index("ix_refund_outbox_events_stale_lease", table_name="refund_outbox_events")
    op.drop_index("ix_refund_outbox_events_due", table_name="refund_outbox_events")
    op.drop_table("refund_outbox_events")
    op.drop_index("ix_compensation_events_type_occurred", table_name="compensation_events")
    op.drop_index("ix_compensation_events_refund_occurred", table_name="compensation_events")
    op.drop_index("ix_compensation_events_case_sequence", table_name="compensation_events")
    op.drop_index("ix_compensation_events_transaction_occurred", table_name="compensation_events")
    op.drop_table("compensation_events")
    op.drop_index("uq_payment_refunds_one_active_per_case", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_state_created_at", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_case_created_at", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_transaction_created_at", table_name="payment_refunds")
    op.drop_table("payment_refunds")
    op.drop_index("ix_compensation_cases_state_created_at", table_name="compensation_cases")
    op.drop_index("ix_compensation_cases_transaction", table_name="compensation_cases")
    op.drop_index("ix_compensation_cases_account_created_at", table_name="compensation_cases")
    op.drop_table("compensation_cases")
