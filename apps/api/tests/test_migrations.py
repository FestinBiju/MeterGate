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
            "alembic_version",
            "buyer_policies",
            "merchants",
            "policy_evaluations",
            "quotes",
            "services",
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
