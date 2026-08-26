import json
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command

API_ROOT = Path(__file__).resolve().parents[1]


def test_migrations_upgrade_and_downgrade_with_injected_connection() -> None:
    engine = create_engine("sqlite://")
    config = Config(API_ROOT / "alembic.ini")

    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

        inspector = inspect(connection)
        assert set(inspector.get_table_names()) == {
            "accounts",
            "alembic_version",
            "approval_identities",
            "buyer_policies",
            "commerce_outbox_events",
            "compensation_cases",
            "compensation_events",
            "entitlements",
            "fulfillment_events",
            "fulfillment_executions",
            "merchants",
            "passkey_credentials",
            "payment_attempts",
            "payment_refunds",
            "payment_transaction_events",
            "payment_transactions",
            "policy_evaluations",
            "purchase_authorizations",
            "quotes",
            "razorpay_webhook_events",
            "refund_outbox_events",
            "service_fulfillment_configs",
            "services",
        }
        account_columns = {column["name"] for column in inspector.get_columns("accounts")}
        assert account_columns == {
            "id",
            "display_name",
            "status",
            "session_version",
            "created_at",
            "updated_at",
        }
        assert {index["name"] for index in inspector.get_indexes("accounts")} >= {
            "ix_accounts_status_created_at"
        }
        assert {index["name"] for index in inspector.get_indexes("services")} >= {
            "ix_services_catalog",
            "ix_services_merchant_list",
        }
        service_foreign_key = inspector.get_foreign_keys("services")[0]
        assert service_foreign_key["referred_table"] == "merchants"
        assert service_foreign_key["options"]["ondelete"] == "RESTRICT"

        quote_indexes = {index["name"] for index in inspector.get_indexes("quotes")}
        assert {
            "ix_quotes_expires_at",
            "ix_quotes_merchant_issued_at",
            "ix_quotes_service_issued_at",
        } <= quote_indexes
        quote_foreign_keys = {
            foreign_key["referred_table"]: foreign_key["options"]["ondelete"]
            for foreign_key in inspector.get_foreign_keys("quotes")
        }
        assert quote_foreign_keys == {
            "merchants": "RESTRICT",
            "services": "RESTRICT",
        }
        quote_columns = {column["name"] for column in inspector.get_columns("quotes")}
        assert quote_columns == {
            "id",
            "merchant_id",
            "service_id",
            "input",
            "input_hash",
            "service_snapshot",
            "amount",
            "currency",
            "purchase_type",
            "maximum_fulfillment_seconds",
            "refund_on_fulfillment_failure",
            "issued_at",
            "expires_at",
            "created_at",
            "quote_hash",
        }
        assert "uq_quotes_quote_hash" in {
            constraint["name"] for constraint in inspector.get_unique_constraints("quotes")
        }

        policy_columns = {column["name"] for column in inspector.get_columns("buyer_policies")}
        assert policy_columns == {
            "id",
            "subject_ref",
            "maximum_amount",
            "allowed_currencies",
            "allowed_merchant_ids",
            "allowed_service_ids",
            "allowed_service_types",
            "allowed_purchase_types",
            "issued_at",
            "expires_at",
            "policy_version",
            "policy_hash",
            "created_at",
        }
        assert {index["name"] for index in inspector.get_indexes("buyer_policies")} >= {
            "ix_buyer_policies_expires_at",
            "ix_buyer_policies_subject_issued_at",
        }
        assert "uq_buyer_policies_policy_hash" in {
            constraint["name"] for constraint in inspector.get_unique_constraints("buyer_policies")
        }

        evaluation_columns = {
            column["name"] for column in inspector.get_columns("policy_evaluations")
        }
        assert evaluation_columns == {
            "id",
            "policy_id",
            "quote_id",
            "policy_hash",
            "quote_hash",
            "decision",
            "checks",
            "evaluated_at",
            "evaluation_version",
            "created_at",
        }
        assert {index["name"] for index in inspector.get_indexes("policy_evaluations")} >= {
            "ix_policy_evaluations_decision_evaluated_at",
            "ix_policy_evaluations_policy_evaluated_at",
            "ix_policy_evaluations_quote_evaluated_at",
        }
        evaluation_foreign_keys = {
            foreign_key["referred_table"]: foreign_key["options"]["ondelete"]
            for foreign_key in inspector.get_foreign_keys("policy_evaluations")
        }
        assert evaluation_foreign_keys == {
            "buyer_policies": "RESTRICT",
            "quotes": "RESTRICT",
        }

        identity_columns = {
            column["name"] for column in inspector.get_columns("approval_identities")
        }
        assert identity_columns == {
            "id",
            "account_id",
            "subject_ref",
            "display_name",
            "webauthn_user_handle",
            "status",
            "created_at",
            "updated_at",
        }
        assert {index["name"] for index in inspector.get_indexes("approval_identities")} >= {
            "ix_approval_identities_status_created_at",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("approval_identities")
        } == {
            "uq_approval_identities_account_id",
            "uq_approval_identities_subject_ref",
            "uq_approval_identities_webauthn_user_handle",
        }
        identity_foreign_key = inspector.get_foreign_keys("approval_identities")[0]
        assert identity_foreign_key["referred_table"] == "accounts"
        assert identity_foreign_key["options"]["ondelete"] == "RESTRICT"

        credential_columns = {
            column["name"] for column in inspector.get_columns("passkey_credentials")
        }
        assert credential_columns == {
            "id",
            "approval_identity_id",
            "credential_id",
            "public_key",
            "sign_count",
            "transports",
            "created_at",
            "last_used_at",
        }
        assert {index["name"] for index in inspector.get_indexes("passkey_credentials")} >= {
            "ix_passkey_credentials_identity_created_at",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("passkey_credentials")
        } == {
            "uq_passkey_credentials_credential_id",
            "uq_passkey_credentials_id_approval_identity_id",
        }
        credential_foreign_key = inspector.get_foreign_keys("passkey_credentials")[0]
        assert credential_foreign_key["referred_table"] == "approval_identities"
        assert credential_foreign_key["options"]["ondelete"] == "RESTRICT"

        authorization_columns = {
            column["name"] for column in inspector.get_columns("purchase_authorizations")
        }
        assert authorization_columns == {
            "id",
            "approval_identity_id",
            "passkey_credential_id",
            "evaluation_id",
            "policy_id",
            "policy_hash",
            "quote_id",
            "quote_hash",
            "merchant_id",
            "service_id",
            "subject_ref",
            "amount",
            "currency",
            "purchase_type",
            "review_hash",
            "challenge_hash",
            "authorized_at",
            "expires_at",
            "authorization_version",
            "authorization_hash",
            "created_at",
        }
        assert {index["name"] for index in inspector.get_indexes("purchase_authorizations")} >= {
            "ix_purchase_authorizations_credential_authorized_at",
            "ix_purchase_authorizations_evaluation_authorized_at",
            "ix_purchase_authorizations_expires_at",
            "ix_purchase_authorizations_identity_authorized_at",
            "ix_purchase_authorizations_policy_authorized_at",
            "ix_purchase_authorizations_quote_authorized_at",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("purchase_authorizations")
        } == {
            "uq_purchase_authorizations_authorization_hash",
            "uq_purchase_authorizations_challenge_hash",
        }
        authorization_foreign_keys = {
            foreign_key["referred_table"]: foreign_key["options"]["ondelete"]
            for foreign_key in inspector.get_foreign_keys("purchase_authorizations")
        }
        assert authorization_foreign_keys == {
            "approval_identities": "RESTRICT",
            "buyer_policies": "RESTRICT",
            "merchants": "RESTRICT",
            "passkey_credentials": "RESTRICT",
            "policy_evaluations": "RESTRICT",
            "quotes": "RESTRICT",
            "services": "RESTRICT",
        }

        transaction_columns = {
            column["name"] for column in inspector.get_columns("payment_transactions")
        }
        assert transaction_columns == {
            "id",
            "account_id",
            "authorization_id",
            "authorization_hash",
            "evaluation_id",
            "policy_id",
            "policy_hash",
            "quote_id",
            "quote_hash",
            "merchant_id",
            "service_id",
            "amount",
            "currency",
            "purchase_type",
            "provider",
            "provider_receipt",
            "provider_order_id",
            "provider_order_status",
            "transaction_state",
            "order_creation_attempts",
            "order_creation_started_at",
            "order_created_at",
            "paid_at",
            "last_reconciled_at",
            "payment_binding_version",
            "payment_binding_hash",
            "revision",
            "created_at",
            "updated_at",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("payment_transactions")
        } == {
            "uq_payment_transactions_authorization_id",
            "uq_payment_transactions_id_provider_order_id",
            "uq_payment_transactions_payment_binding_hash",
            "uq_payment_transactions_provider_order_id",
            "uq_payment_transactions_provider_receipt",
        }
        assert {index["name"] for index in inspector.get_indexes("payment_transactions")} >= {
            "ix_payment_transactions_account_created_at",
            "ix_payment_transactions_merchant_created_at",
            "ix_payment_transactions_quote_created_at",
            "ix_payment_transactions_state_updated_at",
        }
        transaction_foreign_keys = {
            foreign_key["referred_table"]: foreign_key["options"]["ondelete"]
            for foreign_key in inspector.get_foreign_keys("payment_transactions")
        }
        assert transaction_foreign_keys == {
            "accounts": "RESTRICT",
            "buyer_policies": "RESTRICT",
            "merchants": "RESTRICT",
            "policy_evaluations": "RESTRICT",
            "purchase_authorizations": "RESTRICT",
            "quotes": "RESTRICT",
            "services": "RESTRICT",
        }

        assert {column["name"] for column in inspector.get_columns("payment_attempts")} == {
            "id",
            "transaction_id",
            "provider",
            "provider_order_id",
            "provider_payment_id",
            "amount",
            "currency",
            "provider_status",
            "method",
            "captured",
            "provider_created_at",
            "first_seen_at",
            "last_seen_at",
            "created_at",
            "updated_at",
        }
        attempt_foreign_key = inspector.get_foreign_keys("payment_attempts")[0]
        assert attempt_foreign_key["referred_table"] == "payment_transactions"
        assert attempt_foreign_key["constrained_columns"] == [
            "transaction_id",
            "provider_order_id",
        ]
        assert attempt_foreign_key["options"]["ondelete"] == "RESTRICT"
        assert {index["name"] for index in inspector.get_indexes("payment_attempts")} >= {
            "ix_payment_attempts_transaction_first_seen_at",
            "uq_payment_attempts_one_captured_per_transaction",
        }

        assert {column["name"] for column in inspector.get_columns("razorpay_webhook_events")} == {
            "id",
            "provider_event_id",
            "provider_event_type",
            "raw_body_hash",
            "provider_created_at",
            "received_at",
            "processed_at",
            "processing_status",
            "processing_reason_code",
            "provider_order_id",
            "provider_payment_id",
            "transaction_id",
            "created_at",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("razorpay_webhook_events")
        } == {"uq_razorpay_webhook_events_provider_event_id"}
        webhook_foreign_key = inspector.get_foreign_keys("razorpay_webhook_events")[0]
        assert webhook_foreign_key["referred_table"] == "payment_transactions"
        assert webhook_foreign_key["options"]["ondelete"] == "RESTRICT"

        assert {
            column["name"] for column in inspector.get_columns("payment_transaction_events")
        } == {
            "id",
            "transaction_id",
            "transaction_revision",
            "event_type",
            "actor_type",
            "actor_id",
            "prior_state",
            "resulting_state",
            "reason_code",
            "metadata",
            "payment_attempt_id",
            "source_webhook_event_id",
            "idempotency_key",
            "occurred_at",
            "created_at",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("payment_transaction_events")
        } == {
            "uq_payment_transaction_events_idempotency_key",
            "uq_payment_transaction_events_source_webhook_event_id",
            "uq_payment_transaction_events_transaction_revision",
        }
        event_type_check = next(
            constraint
            for constraint in inspector.get_check_constraints("payment_transaction_events")
            if constraint["name"] == "ck_payment_transaction_events_event_type"
        )
        assert "payment_reverified" in event_type_check["sqltext"]
        assert {
            foreign_key["referred_table"]: foreign_key["options"]["ondelete"]
            for foreign_key in inspector.get_foreign_keys("payment_transaction_events")
        } == {
            "payment_attempts": "RESTRICT",
            "payment_transactions": "RESTRICT",
            "razorpay_webhook_events": "RESTRICT",
        }

        entitlement_columns = {column["name"] for column in inspector.get_columns("entitlements")}
        assert {
            "id",
            "transaction_id",
            "payment_binding_hash",
            "payment_reverification_event_id",
            "payment_reverification_revision",
            "provider_order_id",
            "provider_payment_id",
            "input",
            "input_hash",
            "maximum_executions",
            "entitlement_version",
            "entitlement_hash",
            "created_at",
        } <= entitlement_columns
        assert "updated_at" not in entitlement_columns
        assert "uq_entitlements_transaction_id" in {
            constraint["name"] for constraint in inspector.get_unique_constraints("entitlements")
        }
        assert {
            foreign_key["referred_table"]: foreign_key["options"]["ondelete"]
            for foreign_key in inspector.get_foreign_keys("entitlements")
        } == {
            "accounts": "RESTRICT",
            "buyer_policies": "RESTRICT",
            "merchants": "RESTRICT",
            "payment_attempts": "RESTRICT",
            "payment_transaction_events": "RESTRICT",
            "payment_transactions": "RESTRICT",
            "policy_evaluations": "RESTRICT",
            "purchase_authorizations": "RESTRICT",
            "quotes": "RESTRICT",
            "services": "RESTRICT",
        }

        assert {
            "lease_generation",
            "lease_expires_at",
            "processing_started_at",
            "processed_at",
        } <= {column["name"] for column in inspector.get_columns("commerce_outbox_events")}
        assert "uq_commerce_outbox_events_dedup_key" in {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("commerce_outbox_events")
        }
        assert {
            "revision",
            "lease_generation",
            "lease_expires_at",
            "result_json",
            "result_hash",
            "compensation_required",
            "fulfillment_config_id",
            "fulfillment_config_revision",
            "provider_type",
            "endpoint_url",
            "request_timeout_seconds",
            "maximum_attempts",
        } <= {column["name"] for column in inspector.get_columns("fulfillment_executions")}
        assert "uq_fulfillment_executions_entitlement_id" in {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("fulfillment_executions")
        }
        assert {
            foreign_key["referred_table"]: foreign_key["options"]["ondelete"]
            for foreign_key in inspector.get_foreign_keys("fulfillment_executions")
        } == {
            "accounts": "RESTRICT",
            "entitlements": "RESTRICT",
            "merchants": "RESTRICT",
            "payment_transactions": "RESTRICT",
            "service_fulfillment_configs": "RESTRICT",
            "services": "RESTRICT",
        }
        fulfillment_event_columns = {
            column["name"] for column in inspector.get_columns("fulfillment_events")
        }
        assert {"transaction_id", "entitlement_id", "execution_id"} <= fulfillment_event_columns
        assert inspector.get_columns("fulfillment_events")[1]["nullable"] is False

        assert {column["name"] for column in inspector.get_columns("compensation_cases")} == {
            "id",
            "account_id",
            "transaction_id",
            "payment_attempt_id",
            "entitlement_id",
            "fulfillment_execution_id",
            "quote_id",
            "quote_hash",
            "merchant_id",
            "service_id",
            "amount_paid",
            "currency",
            "failure_code",
            "failure_evidence_version",
            "failure_evidence_hash",
            "recommended_action",
            "decision_state",
            "decision_provenance",
            "approved_refund_amount",
            "decision_reason_code",
            "created_at",
            "decided_at",
            "closed_at",
            "revision",
        }
        assert "uq_compensation_cases_fulfillment_execution_id" in {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("compensation_cases")
        }
        assert {
            foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys("compensation_cases")
        } == {
            "accounts",
            "entitlements",
            "fulfillment_executions",
            "merchants",
            "payment_attempts",
            "payment_transactions",
            "quotes",
            "services",
        }

        assert {column["name"] for column in inspector.get_columns("payment_refunds")} == {
            "id",
            "compensation_case_id",
            "transaction_id",
            "payment_attempt_id",
            "provider",
            "provider_payment_id",
            "provider_refund_id",
            "amount",
            "currency",
            "refund_state",
            "provider_status",
            "provider_receipt",
            "created_at",
            "requested_at",
            "processed_at",
            "failed_at",
            "last_reconciled_at",
            "reconciliation_required_at",
            "reconciliation_reason_code",
            "revision",
        }
        assert {
            "uq_payment_refunds_provider_refund_id",
            "uq_payment_refunds_provider_receipt",
        } <= {
            constraint["name"] for constraint in inspector.get_unique_constraints("payment_refunds")
        }
        assert "uq_payment_refunds_one_active_per_case" in {
            index["name"] for index in inspector.get_indexes("payment_refunds")
        }
        refund_checks = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("payment_refunds")
        }
        terminal_provider_evidence = refund_checks["ck_payment_refunds_terminal_provider_evidence"]
        assert "refund_state <> 'refunded' OR provider_status = 'processed'" in (
            terminal_provider_evidence
        )
        assert "provider_refund_id IS NULL AND provider_status IS NULL" in (
            terminal_provider_evidence
        )
        assert "provider_refund_id IS NOT NULL AND provider_status = 'failed'" in (
            terminal_provider_evidence
        )
        assert {
            "ck_payment_refunds_reconciliation_overlay_pair",
            "ck_payment_refunds_reconciliation_required_after_request",
            "ck_payment_refunds_reconciliation_reason_code_valid",
        } <= set(refund_checks)

        compensation_event_checks = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("compensation_events")
        }
        assert (
            "refund_reconciliation_resolved"
            in compensation_event_checks["ck_compensation_events_event_type"]
        )
        assert (
            "refund_reconciliation_resolved"
            in compensation_event_checks["ck_compensation_events_event_binding"]
        )

        assert {
            "sequence",
            "case_revision",
            "refund_revision",
            "metadata",
            "idempotency_key",
        } <= {column["name"] for column in inspector.get_columns("compensation_events")}
        assert {
            "uq_compensation_events_case_sequence",
            "uq_compensation_events_idempotency_key",
        } <= {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("compensation_events")
        }

        assert {
            "lease_generation",
            "lease_expires_at",
            "processing_started_at",
            "processed_at",
        } <= {column["name"] for column in inspector.get_columns("refund_outbox_events")}
        assert {
            "uq_refund_outbox_events_case_id",
            "uq_refund_outbox_events_dedup_key",
        } <= {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("refund_outbox_events")
        }

        reverified_event_id = "pte_00000000000000000000000001"
        connection.exec_driver_sql(
            """
            INSERT INTO payment_transaction_events (
                id,
                transaction_id,
                transaction_revision,
                event_type,
                actor_type,
                actor_id,
                prior_state,
                resulting_state,
                reason_code,
                metadata,
                payment_attempt_id,
                source_webhook_event_id,
                idempotency_key,
                occurred_at
            ) VALUES (?, ?, 1, 'payment_reverified', 'provider_api', NULL, NULL,
                      'paid', 'PAYMENT_REVERIFIED', ?, NULL, NULL, ?, ?)
            """,
            (
                reverified_event_id,
                "txn_00000000000000000000000001",
                '{"provider_status":"captured"}',
                "migration:payment-reverified",
                "2026-08-26 00:00:00+00:00",
            ),
        )
        event_before_downgrade = connection.exec_driver_sql(
            """
            SELECT id, transaction_id, transaction_revision, event_type, actor_type,
                   actor_id, prior_state, resulting_state, reason_code, metadata,
                   payment_attempt_id, source_webhook_event_id, idempotency_key, occurred_at
            FROM payment_transaction_events
            WHERE id = ?
            """,
            (reverified_event_id,),
        ).one()

        command.downgrade(config, "20260825_0006")
        assert {
            "commerce_outbox_events",
            "compensation_cases",
            "compensation_events",
            "entitlements",
            "fulfillment_events",
            "fulfillment_executions",
            "payment_refunds",
            "refund_outbox_events",
            "service_fulfillment_configs",
        }.isdisjoint(inspect(connection).get_table_names())
        event_after_downgrade = connection.exec_driver_sql(
            """
            SELECT id, transaction_id, transaction_revision, event_type, actor_type,
                   actor_id, prior_state, resulting_state, reason_code, metadata,
                   payment_attempt_id, source_webhook_event_id, idempotency_key, occurred_at
            FROM payment_transaction_events
            WHERE id = ?
            """,
            (reverified_event_id,),
        ).one()
        assert event_after_downgrade[:9] == (
            *event_before_downgrade[:3],
            "payment_reconciled",
            *event_before_downgrade[4:9],
        )
        assert event_after_downgrade[10:] == event_before_downgrade[10:]
        downgraded_metadata = json.loads(event_after_downgrade[9])
        assert downgraded_metadata["provider_status"] == "captured"
        assert downgraded_metadata["_metergate_migration"] == {
            "20260826_0007_downgrade": {
                "event_type_from": "payment_reverified",
                "event_type_to": "payment_reconciled",
            }
        }
        downgraded_event_type_check = next(
            constraint
            for constraint in inspect(connection).get_check_constraints(
                "payment_transaction_events"
            )
            if constraint["name"] == "ck_payment_transaction_events_event_type"
        )
        assert "payment_reverified" not in downgraded_event_type_check["sqltext"]
        assert "payment_reconciled" in downgraded_event_type_check["sqltext"]
        command.upgrade(config, "head")

        command.downgrade(config, "20260825_0005")
        milestone_six_a_tables = set(inspect(connection).get_table_names())
        assert {
            "payment_attempts",
            "payment_transaction_events",
            "payment_transactions",
            "razorpay_webhook_events",
        }.isdisjoint(milestone_six_a_tables)
        assert "purchase_authorizations" in milestone_six_a_tables

        command.upgrade(config, "head")
        assert {
            "payment_attempts",
            "payment_transaction_events",
            "payment_transactions",
            "razorpay_webhook_events",
        } <= set(inspect(connection).get_table_names())

        command.downgrade(config, "20260825_0004")
        assert set(inspect(connection).get_table_names()) == {
            "alembic_version",
            "approval_identities",
            "buyer_policies",
            "merchants",
            "passkey_credentials",
            "policy_evaluations",
            "purchase_authorizations",
            "quotes",
            "services",
        }
        assert "account_id" not in {
            column["name"] for column in inspect(connection).get_columns("approval_identities")
        }

        command.upgrade(config, "head")
        assert "accounts" in inspect(connection).get_table_names()

        command.downgrade(config, "20260825_0003")
        assert set(inspect(connection).get_table_names()) == {
            "alembic_version",
            "buyer_policies",
            "merchants",
            "policy_evaluations",
            "quotes",
            "services",
        }

        command.upgrade(config, "head")
        assert {
            "approval_identities",
            "passkey_credentials",
            "purchase_authorizations",
        } <= set(inspect(connection).get_table_names())

        command.downgrade(config, "20260825_0002")
        assert set(inspect(connection).get_table_names()) == {
            "alembic_version",
            "merchants",
            "quotes",
            "services",
        }

        command.upgrade(config, "head")
        assert {
            "buyer_policies",
            "policy_evaluations",
        } <= set(inspect(connection).get_table_names())

        command.downgrade(config, "20260825_0001")
        assert set(inspect(connection).get_table_names()) == {
            "alembic_version",
            "merchants",
            "services",
        }

        command.upgrade(config, "head")
        assert "quotes" in inspect(connection).get_table_names()

        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]

    engine.dispose()


def test_account_migration_backfills_legacy_identities_without_changing_subjects() -> None:
    engine = create_engine("sqlite://")
    config = Config(API_ROOT / "alembic.ini")
    identities = (
        (
            "aid_00000000000000000000000001",
            "legacy-active",
            "Legacy active",
            b"a" * 32,
            "active",
        ),
        (
            "aid_00000000000000000000000002",
            "legacy-pending",
            "Legacy pending",
            b"b" * 32,
            "active",
        ),
        (
            "aid_00000000000000000000000003",
            "legacy-disabled",
            "Legacy disabled",
            b"c" * 32,
            "disabled",
        ),
    )

    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "20260825_0004")
        for identity in identities:
            connection.exec_driver_sql(
                """
                INSERT INTO approval_identities (
                    id, subject_ref, display_name, webauthn_user_handle, status
                ) VALUES (?, ?, ?, ?, ?)
                """,
                identity,
            )
        for index, identity_id in enumerate((identities[0][0], identities[2][0]), start=1):
            connection.exec_driver_sql(
                """
                INSERT INTO passkey_credentials (
                    id, approval_identity_id, credential_id, public_key, sign_count, transports
                ) VALUES (?, ?, ?, ?, 0, NULL)
                """,
                (
                    f"pkc_0000000000000000000000000{index}",
                    identity_id,
                    f"credential-{index}".encode(),
                    f"public-key-{index}".encode(),
                ),
            )
        connection.commit()

        command.upgrade(config, "head")
        backfilled = connection.exec_driver_sql(
            """
            SELECT account.id, account.status, identity.subject_ref, identity.account_id
            FROM accounts AS account
            JOIN approval_identities AS identity ON identity.account_id = account.id
            ORDER BY account.id
            """
        ).all()
        assert backfilled == [
            (
                "acct_00000000000000000000000001",
                "active",
                "legacy-active",
                "acct_00000000000000000000000001",
            ),
            (
                "acct_00000000000000000000000002",
                "pending",
                "legacy-pending",
                "acct_00000000000000000000000002",
            ),
            (
                "acct_00000000000000000000000003",
                "disabled",
                "legacy-disabled",
                "acct_00000000000000000000000003",
            ),
        ]

        command.downgrade(config, "20260825_0004")
        assert "accounts" not in inspect(connection).get_table_names()
        preserved_subjects = connection.exec_driver_sql(
            "SELECT id, subject_ref FROM approval_identities ORDER BY id"
        ).all()
        assert preserved_subjects == [(identity[0], identity[1]) for identity in identities]

    engine.dispose()
