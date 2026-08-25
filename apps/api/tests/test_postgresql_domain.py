import asyncio
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.application import create_app
from app.core.config import Settings, get_settings
from app.db.session import Database, make_async_database_url
from app.domain.approval_hashing import AUTHORIZATION_VERSION, calculate_authorization_hash
from app.domain.enums import PurchaseType
from app.domain.hashing import sha256_bytes
from app.domain.ids import new_authorization_id, new_policy_evaluation_id, new_policy_id
from app.models import PasskeyCredential, PurchaseAuthorization
from app.repositories.authorizations import PurchaseAuthorizationRepository
from app.repositories.passkey_credentials import PasskeyCredentialRepository
from app.scripts.seed_dev import seed_development_data
from app.services.readiness import ReadinessService

API_ROOT = Path(__file__).resolve().parents[1]
ID_PATTERN = re.compile(r"^(mrc_|pol_|pye_|qte_|svc_)[0-7][0-9A-HJKMNP-TV-Z]{25}$")
TEST_SCHEMA_PATTERN = re.compile(r"^metergate_test_[0-9a-f]{32}$")


async def healthy_probe() -> None:
    return None


@dataclass(frozen=True, slots=True)
class IsolatedDatabase:
    database: Database
    database_url: str
    schema: str


def _database_url_for_tests() -> str:
    explicit_url = os.getenv("TEST_DATABASE_URL")
    if explicit_url:
        return explicit_url

    database_url = get_settings().database_url.get_secret_value()
    host = make_async_database_url(database_url).host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail(
            "Refusing to create an isolated test schema on a non-loopback DATABASE_URL. "
            "Set TEST_DATABASE_URL explicitly to authorize a dedicated test database."
        )
    return database_url


def _alembic_upgrade(connection: Any) -> None:
    config = Config(API_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


def _alembic_check(connection: Any) -> None:
    config = Config(API_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    command.check(config)


def _alembic_downgrade_to_milestone_two(connection: Any) -> None:
    config = Config(API_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    command.downgrade(config, "20260825_0001")


def _alembic_downgrade_to_milestone_three(connection: Any) -> None:
    config = Config(API_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    command.downgrade(config, "20260825_0002")


def _alembic_downgrade_to_milestone_four(connection: Any) -> None:
    config = Config(API_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    command.downgrade(config, "20260825_0003")


async def _prepare_isolated_database() -> IsolatedDatabase:
    database_url = _database_url_for_tests()
    async_url = make_async_database_url(database_url)
    schema = f"metergate_test_{uuid.uuid4().hex}"
    assert TEST_SCHEMA_PATTERN.fullmatch(schema)

    control_engine = create_async_engine(
        async_url,
        hide_parameters=True,
        poolclass=NullPool,
    )
    try:
        async with control_engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
    finally:
        await control_engine.dispose()

    test_engine = create_async_engine(
        async_url,
        connect_args={"server_settings": {"search_path": schema}},
        hide_parameters=True,
        pool_pre_ping=True,
        poolclass=NullPool,
    )
    database = Database(test_engine)
    try:
        async with test_engine.connect() as connection:
            await connection.run_sync(_alembic_upgrade)
    except Exception:
        await database.dispose()
        await _drop_isolated_schema(database_url, schema)
        raise

    return IsolatedDatabase(
        database=database,
        database_url=database_url,
        schema=schema,
    )


async def _drop_isolated_schema(database_url: str, schema: str) -> None:
    if not TEST_SCHEMA_PATTERN.fullmatch(schema):
        raise RuntimeError("Refusing to drop an unexpected test schema")

    control_engine = create_async_engine(
        make_async_database_url(database_url),
        hide_parameters=True,
        poolclass=NullPool,
    )
    try:
        async with control_engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
    finally:
        await control_engine.dispose()


@pytest.fixture(scope="session")
def isolated_database() -> Iterator[IsolatedDatabase]:
    isolated = asyncio.run(_prepare_isolated_database())
    yield isolated
    asyncio.run(isolated.database.dispose())
    asyncio.run(_drop_isolated_schema(isolated.database_url, isolated.schema))


@pytest.fixture(scope="session")
def domain_client(isolated_database: IsolatedDatabase) -> Iterator[TestClient]:
    settings = Settings(
        service_name="metergate-api",
        log_level="INFO",
        healthcheck_timeout_seconds=2,
        database_url="postgresql://unused:unused@localhost:5432/unused",
        redis_url="redis://localhost:6379/15",
        cors_allowed_origins=["http://localhost:3000"],
        _env_file=None,
    )
    readiness_service = ReadinessService(
        postgresql_probe=healthy_probe,
        redis_probe=healthy_probe,
        timeout_seconds=0.1,
    )
    with TestClient(
        create_app(
            settings=settings,
            readiness_service=readiness_service,
            database=isolated_database.database,
        )
    ) as client:
        yield client


def merchant_payload(
    slug: str,
    *,
    status: str = "active",
) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "description": f"Synthetic development merchant for {slug}.",
        "status": status,
    }


def service_payload(
    slug: str,
    *,
    status: str = "active",
    base_price: int = 500,
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "description": f"Synthetic development service for {slug}.",
        "status": status,
        "service_type": "report",
        "purchase_type": "one_time",
        "currency": "INR",
        "base_price": base_price,
        "input_schema": input_schema
        if input_schema is not None
        else {
            "type": "object",
            "properties": {"norad_id": {"type": "integer"}},
            "required": ["norad_id"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"risk_level": {"type": "string"}},
        },
        "output_content_type": "application/json",
        "maximum_fulfillment_seconds": 30,
        "refund_on_fulfillment_failure": True,
    }


def create_merchant(
    client: TestClient,
    slug: str,
    *,
    status: str = "active",
) -> dict[str, Any]:
    response = client.post("/api/v1/merchants", json=merchant_payload(slug, status=status))
    assert response.status_code == 201, response.text
    return response.json()


def create_service(
    client: TestClient,
    merchant_id: str,
    slug: str,
    *,
    status: str = "active",
    base_price: int = 500,
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/merchants/{merchant_id}/services",
        json=service_payload(
            slug,
            status=status,
            base_price=base_price,
            input_schema=input_schema,
        ),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_alembic_upgraded_a_clean_postgresql_schema(
    isolated_database: IsolatedDatabase,
) -> None:
    async def inspect_database() -> tuple[str, set[str], set[str], set[str]]:
        async with isolated_database.database.engine.connect() as connection:

            def inspect_connection(
                sync_connection: Any,
            ) -> tuple[str, set[str], set[str], set[str]]:
                inspector = inspect(sync_connection)
                tables = set(inspector.get_table_names())
                checks = {item["name"] for item in inspector.get_check_constraints("services")}
                unique_constraints = {
                    item["name"] for item in inspector.get_unique_constraints("services")
                }
                foreign_keys = {item["name"] for item in inspector.get_foreign_keys("services")}
                return (
                    sync_connection.dialect.name,
                    tables,
                    checks,
                    unique_constraints | foreign_keys,
                )

            return await connection.run_sync(inspect_connection)

    dialect, tables, checks, named_constraints = asyncio.run(inspect_database())

    assert dialect == "postgresql"
    assert {
        "alembic_version",
        "approval_identities",
        "buyer_policies",
        "merchants",
        "passkey_credentials",
        "policy_evaluations",
        "purchase_authorizations",
        "quotes",
        "services",
    } <= tables
    assert {
        "ck_services_service_base_price_nonnegative",
        "ck_services_service_input_schema_object",
        "ck_services_service_output_schema_object",
    } <= checks
    assert "uq_services_merchant_id_slug" in named_constraints
    assert "fk_services_merchant_id_merchants" in named_constraints


def test_quote_migration_has_postgresql_integrity_guards(
    isolated_database: IsolatedDatabase,
) -> None:
    async def inspect_quotes() -> dict[str, Any]:
        async with isolated_database.database.engine.connect() as connection:

            def inspect_connection(sync_connection: Any) -> dict[str, Any]:
                inspector = inspect(sync_connection)
                return {
                    "columns": {
                        column["name"]: str(column["type"])
                        for column in inspector.get_columns("quotes")
                    },
                    "checks": {item["name"] for item in inspector.get_check_constraints("quotes")},
                    "foreign_keys": {
                        item["referred_table"]: item["options"].get("ondelete")
                        for item in inspector.get_foreign_keys("quotes")
                    },
                    "indexes": {item["name"] for item in inspector.get_indexes("quotes")},
                    "unique": {item["name"] for item in inspector.get_unique_constraints("quotes")},
                }

            inspected = await connection.run_sync(inspect_connection)
            triggers = await connection.scalars(
                text(
                    """
                    SELECT trigger_name
                    FROM information_schema.triggers
                    WHERE event_object_schema = current_schema()
                      AND event_object_table = 'quotes'
                    """
                )
            )
            inspected["triggers"] = set(triggers.all())
            return inspected

    quote_schema = asyncio.run(inspect_quotes())

    assert quote_schema["columns"] == {
        "id": "VARCHAR(30)",
        "merchant_id": "VARCHAR(30)",
        "service_id": "VARCHAR(30)",
        "input": "JSONB",
        "input_hash": "VARCHAR(71)",
        "service_snapshot": "JSONB",
        "amount": "BIGINT",
        "currency": "VARCHAR(3)",
        "purchase_type": "VARCHAR(12)",
        "maximum_fulfillment_seconds": "INTEGER",
        "refund_on_fulfillment_failure": "BOOLEAN",
        "issued_at": "TIMESTAMP",
        "expires_at": "TIMESTAMP",
        "created_at": "TIMESTAMP",
        "quote_hash": "VARCHAR(71)",
    }
    assert {
        "ck_quotes_quote_amount_safe_integer_range",
        "ck_quotes_quote_expiry_after_issue",
        "ck_quotes_quote_hash_format",
        "ck_quotes_quote_input_hash_format",
        "ck_quotes_quote_service_snapshot_object",
    } <= quote_schema["checks"]
    assert quote_schema["foreign_keys"] == {
        "merchants": "RESTRICT",
        "services": "RESTRICT",
    }
    assert {
        "ix_quotes_expires_at",
        "ix_quotes_merchant_issued_at",
        "ix_quotes_service_issued_at",
    } <= quote_schema["indexes"]
    assert "uq_quotes_quote_hash" in quote_schema["unique"]
    assert quote_schema["triggers"] == {"trg_quotes_immutable"}


def test_quote_migration_downgrades_and_reupgrades_in_disposable_postgresql_schema() -> None:
    async def exercise_cycle() -> None:
        isolated = await _prepare_isolated_database()
        try:
            async with isolated.database.engine.connect() as connection:
                await connection.run_sync(_alembic_downgrade_to_milestone_two)

                def milestone_two_tables(sync_connection: Any) -> set[str]:
                    return set(inspect(sync_connection).get_table_names())

                assert await connection.run_sync(milestone_two_tables) == {
                    "alembic_version",
                    "merchants",
                    "services",
                }

                await connection.run_sync(_alembic_upgrade)

                def milestone_three_tables(sync_connection: Any) -> set[str]:
                    return set(inspect(sync_connection).get_table_names())

                assert "quotes" in await connection.run_sync(milestone_three_tables)
        finally:
            await isolated.database.dispose()
            await _drop_isolated_schema(isolated.database_url, isolated.schema)

    asyncio.run(exercise_cycle())


def test_policy_migration_has_postgresql_integrity_guards(
    isolated_database: IsolatedDatabase,
) -> None:
    async def inspect_policy_tables() -> dict[str, Any]:
        async with isolated_database.database.engine.connect() as connection:

            def inspect_connection(sync_connection: Any) -> dict[str, Any]:
                inspector = inspect(sync_connection)
                return {
                    "policy_columns": {
                        column["name"]: str(column["type"])
                        for column in inspector.get_columns("buyer_policies")
                    },
                    "policy_checks": {
                        item["name"] for item in inspector.get_check_constraints("buyer_policies")
                    },
                    "policy_indexes": {
                        item["name"] for item in inspector.get_indexes("buyer_policies")
                    },
                    "policy_unique": {
                        item["name"] for item in inspector.get_unique_constraints("buyer_policies")
                    },
                    "evaluation_columns": {
                        column["name"]: str(column["type"])
                        for column in inspector.get_columns("policy_evaluations")
                    },
                    "evaluation_checks": {
                        item["name"]
                        for item in inspector.get_check_constraints("policy_evaluations")
                    },
                    "evaluation_foreign_keys": {
                        item["referred_table"]: item["options"].get("ondelete")
                        for item in inspector.get_foreign_keys("policy_evaluations")
                    },
                    "evaluation_indexes": {
                        item["name"] for item in inspector.get_indexes("policy_evaluations")
                    },
                }

            inspected = await connection.run_sync(inspect_connection)
            trigger_rows = await connection.execute(
                text(
                    """
                    SELECT event_object_table, trigger_name
                    FROM information_schema.triggers
                    WHERE event_object_schema = current_schema()
                      AND event_object_table IN ('buyer_policies', 'policy_evaluations')
                    """
                )
            )
            inspected["triggers"] = set(trigger_rows.tuples().all())
            return inspected

    schema = asyncio.run(inspect_policy_tables())

    assert schema["policy_columns"] == {
        "id": "VARCHAR(30)",
        "subject_ref": "VARCHAR(200)",
        "maximum_amount": "BIGINT",
        "allowed_currencies": "JSONB",
        "allowed_merchant_ids": "JSONB",
        "allowed_service_ids": "JSONB",
        "allowed_service_types": "JSONB",
        "allowed_purchase_types": "JSONB",
        "issued_at": "TIMESTAMP",
        "expires_at": "TIMESTAMP",
        "policy_version": "VARCHAR(16)",
        "policy_hash": "VARCHAR(71)",
        "created_at": "TIMESTAMP",
    }
    assert {
        "ck_buyer_policies_policy_allowed_currencies_valid_allowlist",
        "ck_buyer_policies_policy_allowed_merchant_ids_valid_allowlist",
        "ck_buyer_policies_policy_allowed_purchase_types_valid_allowlist",
        "ck_buyer_policies_policy_allowed_service_ids_valid_allowlist",
        "ck_buyer_policies_policy_allowed_service_types_valid_allowlist",
        "ck_buyer_policies_policy_expiry_after_issue",
        "ck_buyer_policies_policy_hash_format",
        "ck_buyer_policies_policy_maximum_amount_safe_integer_range",
        "ck_buyer_policies_policy_subject_ref_normalized_nonblank",
        "ck_buyer_policies_policy_version",
    } <= schema["policy_checks"]
    assert schema["policy_unique"] == {"uq_buyer_policies_policy_hash"}
    assert {
        "ix_buyer_policies_expires_at",
        "ix_buyer_policies_subject_issued_at",
    } <= schema["policy_indexes"]

    assert schema["evaluation_columns"] == {
        "id": "VARCHAR(30)",
        "policy_id": "VARCHAR(30)",
        "quote_id": "VARCHAR(30)",
        "policy_hash": "VARCHAR(71)",
        "quote_hash": "VARCHAR(71)",
        "decision": "VARCHAR(5)",
        "checks": "JSONB",
        "evaluated_at": "TIMESTAMP",
        "evaluation_version": "VARCHAR(16)",
        "created_at": "TIMESTAMP",
    }
    assert {
        "ck_policy_evaluations_checks_nonempty_object_array",
        "ck_policy_evaluations_policy_evaluation_decision",
        "ck_policy_evaluations_policy_evaluation_policy_hash_format",
        "ck_policy_evaluations_policy_evaluation_quote_hash_format",
        "ck_policy_evaluations_policy_evaluation_version",
    } <= schema["evaluation_checks"]
    assert schema["evaluation_foreign_keys"] == {
        "buyer_policies": "RESTRICT",
        "quotes": "RESTRICT",
    }
    assert {
        "ix_policy_evaluations_decision_evaluated_at",
        "ix_policy_evaluations_policy_evaluated_at",
        "ix_policy_evaluations_quote_evaluated_at",
    } <= schema["evaluation_indexes"]
    assert schema["triggers"] == {
        ("buyer_policies", "trg_buyer_policies_immutable"),
        ("buyer_policies", "trg_buyer_policies_validate_allowlists"),
        ("policy_evaluations", "trg_policy_evaluations_immutable"),
    }

    async def check_model_drift() -> None:
        async with isolated_database.database.engine.connect() as connection:
            await connection.run_sync(_alembic_check)

    asyncio.run(check_model_drift())


def test_policy_migration_downgrades_to_milestone_three_and_reupgrades() -> None:
    async def exercise_cycle() -> None:
        isolated = await _prepare_isolated_database()
        try:
            async with isolated.database.engine.connect() as connection:
                await connection.run_sync(_alembic_downgrade_to_milestone_three)

                def milestone_three_tables(sync_connection: Any) -> set[str]:
                    return set(inspect(sync_connection).get_table_names())

                assert await connection.run_sync(milestone_three_tables) == {
                    "alembic_version",
                    "merchants",
                    "quotes",
                    "services",
                }
                mutation_function = await connection.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_proc
                        JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
                        WHERE pg_namespace.nspname = current_schema()
                          AND pg_proc.proname = 'metergate_reject_policy_record_mutation'
                        """
                    )
                )
                assert mutation_function == 0

                await connection.run_sync(_alembic_upgrade)
                upgraded_tables = await connection.run_sync(milestone_three_tables)
                assert {"buyer_policies", "policy_evaluations"} <= upgraded_tables
        finally:
            await isolated.database.dispose()
            await _drop_isolated_schema(isolated.database_url, isolated.schema)

    asyncio.run(exercise_cycle())


def test_approval_migration_has_postgresql_integrity_guards(
    isolated_database: IsolatedDatabase,
) -> None:
    async def inspect_approval_tables() -> dict[str, Any]:
        async with isolated_database.database.engine.connect() as connection:

            def inspect_connection(sync_connection: Any) -> dict[str, Any]:
                inspector = inspect(sync_connection)
                return {
                    "identity_columns": {
                        column["name"]: str(column["type"])
                        for column in inspector.get_columns("approval_identities")
                    },
                    "identity_checks": {
                        item["name"]
                        for item in inspector.get_check_constraints("approval_identities")
                    },
                    "identity_indexes": {
                        item["name"] for item in inspector.get_indexes("approval_identities")
                    },
                    "identity_unique": {
                        item["name"]
                        for item in inspector.get_unique_constraints("approval_identities")
                    },
                    "credential_columns": {
                        column["name"]: str(column["type"])
                        for column in inspector.get_columns("passkey_credentials")
                    },
                    "credential_checks": {
                        item["name"]
                        for item in inspector.get_check_constraints("passkey_credentials")
                    },
                    "credential_foreign_keys": {
                        item["referred_table"]: item["options"].get("ondelete")
                        for item in inspector.get_foreign_keys("passkey_credentials")
                    },
                    "credential_indexes": {
                        item["name"] for item in inspector.get_indexes("passkey_credentials")
                    },
                    "credential_unique": {
                        item["name"]
                        for item in inspector.get_unique_constraints("passkey_credentials")
                    },
                    "authorization_columns": {
                        column["name"]: str(column["type"])
                        for column in inspector.get_columns("purchase_authorizations")
                    },
                    "authorization_checks": {
                        item["name"]
                        for item in inspector.get_check_constraints("purchase_authorizations")
                    },
                    "authorization_foreign_keys": {
                        item["referred_table"]: item["options"].get("ondelete")
                        for item in inspector.get_foreign_keys("purchase_authorizations")
                    },
                    "authorization_indexes": {
                        item["name"] for item in inspector.get_indexes("purchase_authorizations")
                    },
                    "authorization_unique": {
                        item["name"]
                        for item in inspector.get_unique_constraints("purchase_authorizations")
                    },
                }

            inspected = await connection.run_sync(inspect_connection)
            trigger_rows = await connection.execute(
                text(
                    """
                    SELECT event_object_table, trigger_name
                    FROM information_schema.triggers
                    WHERE event_object_schema = current_schema()
                      AND event_object_table IN (
                          'approval_identities',
                          'passkey_credentials',
                          'purchase_authorizations'
                      )
                    """
                )
            )
            inspected["triggers"] = set(trigger_rows.tuples().all())
            return inspected

    schema = asyncio.run(inspect_approval_tables())

    assert schema["identity_columns"] == {
        "id": "VARCHAR(30)",
        "subject_ref": "VARCHAR(200)",
        "display_name": "VARCHAR(200)",
        "webauthn_user_handle": "BYTEA",
        "status": "VARCHAR(8)",
        "created_at": "TIMESTAMP",
        "updated_at": "TIMESTAMP",
    }
    assert {
        "ck_approval_identities_id_format",
        "ck_approval_identities_status",
        "ck_approval_identities_subject_ref_valid",
        "ck_approval_identities_webauthn_user_handle_length",
    } <= schema["identity_checks"]
    assert schema["identity_indexes"] >= {"ix_approval_identities_status_created_at"}
    assert schema["identity_unique"] == {
        "uq_approval_identities_subject_ref",
        "uq_approval_identities_webauthn_user_handle",
    }

    assert schema["credential_columns"] == {
        "id": "VARCHAR(30)",
        "approval_identity_id": "VARCHAR(30)",
        "credential_id": "BYTEA",
        "public_key": "BYTEA",
        "sign_count": "BIGINT",
        "transports": "JSONB",
        "created_at": "TIMESTAMP",
        "last_used_at": "TIMESTAMP",
    }
    assert {
        "ck_passkey_credentials_credential_id_length",
        "ck_passkey_credentials_id_format",
        "ck_passkey_credentials_public_key_length",
        "ck_passkey_credentials_sign_count_range",
        "ck_passkey_credentials_transports_valid",
    } <= schema["credential_checks"]
    assert schema["credential_foreign_keys"] == {"approval_identities": "RESTRICT"}
    assert schema["credential_indexes"] >= {"ix_passkey_credentials_identity_created_at"}
    assert schema["credential_unique"] == {
        "uq_passkey_credentials_credential_id",
        "uq_passkey_credentials_id_approval_identity_id",
    }

    assert schema["authorization_columns"] == {
        "id": "VARCHAR(30)",
        "approval_identity_id": "VARCHAR(30)",
        "passkey_credential_id": "VARCHAR(30)",
        "evaluation_id": "VARCHAR(30)",
        "policy_id": "VARCHAR(30)",
        "policy_hash": "VARCHAR(71)",
        "quote_id": "VARCHAR(30)",
        "quote_hash": "VARCHAR(71)",
        "merchant_id": "VARCHAR(30)",
        "service_id": "VARCHAR(30)",
        "subject_ref": "VARCHAR(200)",
        "amount": "BIGINT",
        "currency": "VARCHAR(3)",
        "purchase_type": "VARCHAR(12)",
        "review_hash": "VARCHAR(71)",
        "challenge_hash": "VARCHAR(71)",
        "authorized_at": "TIMESTAMP",
        "expires_at": "TIMESTAMP",
        "authorization_version": "VARCHAR(16)",
        "authorization_hash": "VARCHAR(71)",
        "created_at": "TIMESTAMP",
    }
    assert {
        "ck_purchase_authorizations_amount_safe_integer_range",
        "ck_purchase_authorizations_authorization_hash_format",
        "ck_purchase_authorizations_challenge_hash_format",
        "ck_purchase_authorizations_expiry_after_authorization",
        "ck_purchase_authorizations_id_format",
        "ck_purchase_authorizations_review_hash_format",
        "ck_purchase_authorizations_version",
    } <= schema["authorization_checks"]
    assert schema["authorization_foreign_keys"] == {
        "approval_identities": "RESTRICT",
        "buyer_policies": "RESTRICT",
        "merchants": "RESTRICT",
        "passkey_credentials": "RESTRICT",
        "policy_evaluations": "RESTRICT",
        "quotes": "RESTRICT",
        "services": "RESTRICT",
    }
    assert schema["authorization_indexes"] >= {
        "ix_purchase_authorizations_credential_authorized_at",
        "ix_purchase_authorizations_evaluation_authorized_at",
        "ix_purchase_authorizations_expires_at",
        "ix_purchase_authorizations_identity_authorized_at",
        "ix_purchase_authorizations_policy_authorized_at",
        "ix_purchase_authorizations_quote_authorized_at",
    }
    assert schema["authorization_unique"] == {
        "uq_purchase_authorizations_authorization_hash",
        "uq_purchase_authorizations_challenge_hash",
    }
    assert schema["triggers"] == {
        ("approval_identities", "trg_approval_identities_guard_mutation"),
        ("passkey_credentials", "trg_passkey_credentials_guard_mutation"),
        ("purchase_authorizations", "trg_purchase_authorizations_immutable"),
    }


def test_approval_migration_downgrades_to_milestone_four_and_reupgrades() -> None:
    async def exercise_cycle() -> None:
        isolated = await _prepare_isolated_database()
        try:
            async with isolated.database.engine.connect() as connection:
                await connection.run_sync(_alembic_downgrade_to_milestone_four)

                def milestone_four_tables(sync_connection: Any) -> set[str]:
                    return set(inspect(sync_connection).get_table_names())

                assert await connection.run_sync(milestone_four_tables) == {
                    "alembic_version",
                    "buyer_policies",
                    "merchants",
                    "policy_evaluations",
                    "quotes",
                    "services",
                }
                approval_functions = await connection.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_proc
                        JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
                        WHERE pg_namespace.nspname = current_schema()
                          AND pg_proc.proname IN (
                              'metergate_guard_approval_identity_mutation',
                              'metergate_guard_passkey_credential_mutation',
                              'metergate_reject_purchase_authorization_mutation'
                          )
                        """
                    )
                )
                assert approval_functions == 0

                await connection.run_sync(_alembic_upgrade)
                upgraded_tables = await connection.run_sync(milestone_four_tables)
                assert {
                    "approval_identities",
                    "passkey_credentials",
                    "purchase_authorizations",
                } <= upgraded_tables
                await connection.run_sync(_alembic_check)
        finally:
            await isolated.database.dispose()
            await _drop_isolated_schema(isolated.database_url, isolated.schema)

    asyncio.run(exercise_cycle())


def test_approval_records_enforce_atomic_usage_and_database_immutability(
    domain_client: TestClient,
    isolated_database: IsolatedDatabase,
) -> None:
    subject_ref = "postgresql-approval-user"
    merchant = create_merchant(domain_client, "approval-persistence-merchant")
    service = create_service(
        domain_client,
        merchant["id"],
        "approval-persistence-service",
        base_price=500,
    )
    quote_response = domain_client.post(
        "/api/v1/quotes",
        json={"service_id": service["id"], "input": {"norad_id": 25544}},
    )
    assert quote_response.status_code == 201, quote_response.text
    quote = quote_response.json()
    policy_response = domain_client.post(
        "/api/v1/policies",
        json={
            "subject_ref": subject_ref,
            "maximum_amount": 1_000,
            "allowed_currencies": ["INR"],
            "allowed_merchant_ids": [merchant["id"]],
            "allowed_service_ids": [service["id"]],
            "allowed_service_types": ["report"],
            "allowed_purchase_types": ["one_time"],
            "expires_in_seconds": 900,
        },
    )
    assert policy_response.status_code == 201, policy_response.text
    policy = policy_response.json()
    evaluation_response = domain_client.post(
        "/api/v1/policy-evaluations",
        json={"policy_id": policy["id"], "quote_id": quote["id"]},
    )
    assert evaluation_response.status_code == 201, evaluation_response.text
    evaluation = evaluation_response.json()
    assert evaluation["decision"] == "allow"
    identity_response = domain_client.post(
        "/api/v1/approval-identities",
        json={"subject_ref": subject_ref, "display_name": "PostgreSQL Approval User"},
    )
    assert identity_response.status_code == 201, identity_response.text
    identity = identity_response.json()

    raw_credential_id = b"postgresql-approval-credential"

    async def create_credential() -> PasskeyCredential:
        async with isolated_database.database.session() as session:
            return await PasskeyCredentialRepository(session).create(
                PasskeyCredential(
                    approval_identity_id=identity["id"],
                    credential_id=raw_credential_id,
                    public_key=b"postgresql-public-key",
                    sign_count=0,
                    transports=["internal"],
                )
            )

    credential = asyncio.run(create_credential())
    authorized_at = datetime.now(UTC)
    expires_at = authorized_at + timedelta(minutes=2)
    review_hash = sha256_bytes(b"postgresql-approval-review")
    challenge_hash = sha256_bytes(b"postgresql-approval-challenge")

    def build_authorization(authorization_id: str) -> PurchaseAuthorization:
        authorization_hash = calculate_authorization_hash(
            authorization_id=authorization_id,
            authorization_version=AUTHORIZATION_VERSION,
            approval_identity_id=identity["id"],
            passkey_credential_id=credential.id,
            subject_ref=subject_ref,
            evaluation_id=evaluation["id"],
            policy_id=policy["id"],
            policy_hash=policy["policy_hash"],
            quote_id=quote["id"],
            quote_hash=quote["quote_hash"],
            merchant_id=merchant["id"],
            service_id=service["id"],
            amount=quote["pricing"]["amount"],
            currency=quote["pricing"]["currency"],
            purchase_type=PurchaseType(quote["pricing"]["purchase_type"]),
            review_hash=review_hash,
            challenge_hash=challenge_hash,
            authorized_at=authorized_at,
            expires_at=expires_at,
        )
        return PurchaseAuthorization(
            id=authorization_id,
            approval_identity_id=identity["id"],
            passkey_credential_id=credential.id,
            evaluation_id=evaluation["id"],
            policy_id=policy["id"],
            policy_hash=policy["policy_hash"],
            quote_id=quote["id"],
            quote_hash=quote["quote_hash"],
            merchant_id=merchant["id"],
            service_id=service["id"],
            subject_ref=subject_ref,
            amount=quote["pricing"]["amount"],
            currency=quote["pricing"]["currency"],
            purchase_type=PurchaseType(quote["pricing"]["purchase_type"]),
            review_hash=review_hash,
            challenge_hash=challenge_hash,
            authorized_at=authorized_at,
            expires_at=expires_at,
            authorization_version=AUTHORIZATION_VERSION,
            authorization_hash=authorization_hash,
        )

    async def persist_authorization(
        authorization: PurchaseAuthorization,
        *,
        new_sign_count: int,
    ) -> PurchaseAuthorization:
        async with isolated_database.database.session() as session:
            locked = await PasskeyCredentialRepository(session).get_by_credential_id_for_update(
                raw_credential_id,
                identity_id=identity["id"],
            )
            assert locked is not None
            return await PurchaseAuthorizationRepository(
                session
            ).create_with_locked_credential_update(
                authorization,
                credential=locked,
                new_sign_count=new_sign_count,
                last_used_at=authorized_at,
            )

    authorization = asyncio.run(
        persist_authorization(build_authorization(new_authorization_id()), new_sign_count=1)
    )

    with pytest.raises(IntegrityError):
        asyncio.run(
            persist_authorization(build_authorization(new_authorization_id()), new_sign_count=2)
        )

    async def stored_usage_state() -> tuple[int, int]:
        async with isolated_database.database.session() as session:
            persisted_credential = await PasskeyCredentialRepository(session).get(credential.id)
            assert persisted_credential is not None
            authorization_count = await session.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM purchase_authorizations
                    WHERE passkey_credential_id = :credential_id
                    """
                ),
                {"credential_id": credential.id},
            )
            assert authorization_count is not None
            return persisted_credential.sign_count, authorization_count

    assert asyncio.run(stored_usage_state()) == (1, 1)

    async def execute(statement: str, identifier: str) -> None:
        async with isolated_database.database.engine.begin() as connection:
            await connection.execute(text(statement), {"identifier": identifier})

    asyncio.run(
        execute(
            "UPDATE passkey_credentials SET sign_count = 2 WHERE id = :identifier",
            credential.id,
        )
    )
    with pytest.raises(DBAPIError):
        asyncio.run(
            execute(
                "UPDATE passkey_credentials SET sign_count = 1 WHERE id = :identifier",
                credential.id,
            )
        )
    with pytest.raises(DBAPIError):
        asyncio.run(
            execute(
                """
                UPDATE passkey_credentials
                SET public_key = decode('6368616e676564', 'hex')
                WHERE id = :identifier
                """,
                credential.id,
            )
        )
    for statement in (
        "UPDATE purchase_authorizations SET amount = 1 WHERE id = :identifier",
        "DELETE FROM purchase_authorizations WHERE id = :identifier",
    ):
        with pytest.raises(DBAPIError):
            asyncio.run(execute(statement, authorization.id))


def test_policy_rows_enforce_json_constraints_foreign_keys_and_immutability(
    domain_client: TestClient,
    isolated_database: IsolatedDatabase,
) -> None:
    merchant = create_merchant(domain_client, "policy-persistence-merchant")
    service = create_service(
        domain_client,
        merchant["id"],
        "policy-persistence-service",
    )
    quote_response = domain_client.post(
        "/api/v1/quotes",
        json={"service_id": service["id"], "input": {"norad_id": 25544}},
    )
    assert quote_response.status_code == 201, quote_response.text
    quote = quote_response.json()

    policy_id = new_policy_id()
    evaluation_id = new_policy_evaluation_id()
    policy_hash = f"sha256:{'a' * 64}"
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=15)

    async def insert_valid_records() -> tuple[bool, list[dict[str, Any]]]:
        async with isolated_database.database.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO buyer_policies (
                        id, subject_ref, maximum_amount, allowed_currencies,
                        allowed_merchant_ids, allowed_service_ids,
                        allowed_service_types, allowed_purchase_types,
                        issued_at, expires_at, policy_version, policy_hash
                    ) VALUES (
                        :id, :subject_ref, :maximum_amount,
                        CAST(:allowed_currencies AS jsonb),
                        CAST(:allowed_merchant_ids AS jsonb),
                        CAST(:allowed_service_ids AS jsonb),
                        CAST(:allowed_service_types AS jsonb),
                        CAST(:allowed_purchase_types AS jsonb),
                        :issued_at, :expires_at, '1', :policy_hash
                    )
                    """
                ),
                {
                    "id": policy_id,
                    "subject_ref": "dev-user-001",
                    "maximum_amount": 1_000,
                    "allowed_currencies": json.dumps(["INR"]),
                    "allowed_merchant_ids": None,
                    "allowed_service_ids": json.dumps([service["id"]]),
                    "allowed_service_types": json.dumps(["report"]),
                    "allowed_purchase_types": json.dumps(["one_time"]),
                    "issued_at": issued_at,
                    "expires_at": expires_at,
                    "policy_hash": policy_hash,
                },
            )
            checks = [
                {
                    "rule": "MAXIMUM_AMOUNT",
                    "result": "pass",
                    "reason_code": "ALLOW_POLICY_SATISFIED",
                }
            ]
            await connection.execute(
                text(
                    """
                    INSERT INTO policy_evaluations (
                        id, policy_id, quote_id, policy_hash, quote_hash,
                        decision, checks, evaluated_at, evaluation_version
                    ) VALUES (
                        :id, :policy_id, :quote_id, :policy_hash, :quote_hash,
                        'allow', CAST(:checks AS jsonb), :evaluated_at, '1'
                    )
                    """
                ),
                {
                    "id": evaluation_id,
                    "policy_id": policy_id,
                    "quote_id": quote["id"],
                    "policy_hash": policy_hash,
                    "quote_hash": quote["quote_hash"],
                    "checks": json.dumps(checks),
                    "evaluated_at": issued_at,
                },
            )
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT allowed_merchant_ids IS NULL AS unconstrained, checks
                        FROM buyer_policies
                        JOIN policy_evaluations
                          ON policy_evaluations.policy_id = buyer_policies.id
                        WHERE buyer_policies.id = :policy_id
                        """
                    ),
                    {"policy_id": policy_id},
                )
            ).one()
            return bool(row.unconstrained), row.checks

    unconstrained, persisted_checks = asyncio.run(insert_valid_records())
    assert unconstrained is True
    assert persisted_checks[0]["rule"] == "MAXIMUM_AMOUNT"

    async def insert_invalid_policy(
        *,
        maximum_amount: int = 1_000,
        currencies: str = '["INR"]',
        subject_ref: str = "dev-user-002",
        expires_at_value: datetime | None = None,
    ) -> None:
        async with isolated_database.database.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO buyer_policies (
                        id, subject_ref, maximum_amount, allowed_currencies,
                        issued_at, expires_at, policy_version, policy_hash
                    ) VALUES (
                        :id, :subject_ref, :maximum_amount,
                        CAST(:currencies AS jsonb), :issued_at, :expires_at,
                        '1', :policy_hash
                    )
                    """
                ),
                {
                    "id": new_policy_id(),
                    "subject_ref": subject_ref,
                    "maximum_amount": maximum_amount,
                    "currencies": currencies,
                    "issued_at": issued_at,
                    "expires_at": expires_at_value or expires_at,
                    "policy_hash": f"sha256:{uuid.uuid4().hex * 2}",
                },
            )

    for invalid_values in (
        {"currencies": "[]"},
        {"currencies": '["INR", "INR"]'},
        {"currencies": json.dumps(["INR"] * 101)},
        {"currencies": '["inr"]'},
        {"maximum_amount": -1},
        {"maximum_amount": 9_007_199_254_740_992},
        {"subject_ref": " "},
        {"expires_at_value": issued_at},
    ):
        with pytest.raises(DBAPIError):
            asyncio.run(insert_invalid_policy(**invalid_values))

    async def insert_invalid_evaluation(
        *,
        checks: str = '[{"rule":"MAXIMUM_AMOUNT"}]',
        policy_id_value: str = policy_id,
    ) -> None:
        async with isolated_database.database.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO policy_evaluations (
                        id, policy_id, quote_id, policy_hash, quote_hash,
                        decision, checks, evaluated_at, evaluation_version
                    ) VALUES (
                        :id, :policy_id, :quote_id, :policy_hash, :quote_hash,
                        'allow', CAST(:checks AS jsonb), :evaluated_at, '1'
                    )
                    """
                ),
                {
                    "id": new_policy_evaluation_id(),
                    "policy_id": policy_id_value,
                    "quote_id": quote["id"],
                    "policy_hash": policy_hash,
                    "quote_hash": quote["quote_hash"],
                    "checks": checks,
                    "evaluated_at": issued_at,
                },
            )

    for invalid_values in (
        {"checks": "[]"},
        {"checks": "[1]"},
        {"policy_id_value": new_policy_id()},
    ):
        with pytest.raises(DBAPIError):
            asyncio.run(insert_invalid_evaluation(**invalid_values))

    async def mutate(statement: str, identifier: str) -> None:
        async with isolated_database.database.engine.begin() as connection:
            await connection.execute(text(statement), {"identifier": identifier})

    mutation_attempts = (
        (
            "UPDATE buyer_policies SET maximum_amount = 1 WHERE id = :identifier",
            policy_id,
        ),
        ("DELETE FROM buyer_policies WHERE id = :identifier", policy_id),
        (
            "UPDATE policy_evaluations SET decision = 'deny' WHERE id = :identifier",
            evaluation_id,
        ),
        ("DELETE FROM policy_evaluations WHERE id = :identifier", evaluation_id),
    )
    for statement, identifier in mutation_attempts:
        with pytest.raises(DBAPIError):
            asyncio.run(mutate(statement, identifier))


def test_policy_api_persists_allow_and_deny_evaluations(
    domain_client: TestClient,
) -> None:
    merchant = create_merchant(domain_client, "policy-evaluation-merchant")
    service = create_service(
        domain_client,
        merchant["id"],
        "policy-evaluation-service",
        base_price=500,
    )
    quote_response = domain_client.post(
        "/api/v1/quotes",
        json={"service_id": service["id"], "input": {"norad_id": 25544}},
    )
    assert quote_response.status_code == 201, quote_response.text
    quote = quote_response.json()

    common_policy = {
        "subject_ref": "dev-policy-integration",
        "allowed_currencies": ["INR"],
        "allowed_merchant_ids": [merchant["id"]],
        "allowed_service_ids": [service["id"]],
        "allowed_service_types": ["report"],
        "allowed_purchase_types": ["one_time"],
        "expires_in_seconds": 900,
    }
    allow_response = domain_client.post(
        "/api/v1/policies",
        json={**common_policy, "maximum_amount": 1_000},
    )
    deny_response = domain_client.post(
        "/api/v1/policies",
        json={**common_policy, "maximum_amount": 100},
    )
    assert allow_response.status_code == 201, allow_response.text
    assert deny_response.status_code == 201, deny_response.text
    allow_policy = allow_response.json()
    deny_policy = deny_response.json()
    assert ID_PATTERN.fullmatch(allow_policy["id"])
    assert allow_policy["constraints"]["maximum_amount"] == 1_000
    assert allow_policy["state"] == "active"
    assert domain_client.get(f"/api/v1/policies/{allow_policy['id']}").json() == allow_policy

    allow_evaluation_response = domain_client.post(
        "/api/v1/policy-evaluations",
        json={"policy_id": allow_policy["id"], "quote_id": quote["id"]},
    )
    deny_evaluation_response = domain_client.post(
        "/api/v1/policy-evaluations",
        json={"policy_id": deny_policy["id"], "quote_id": quote["id"]},
    )
    assert allow_evaluation_response.status_code == 201, allow_evaluation_response.text
    assert deny_evaluation_response.status_code == 201, deny_evaluation_response.text
    allow_evaluation = allow_evaluation_response.json()
    deny_evaluation = deny_evaluation_response.json()

    assert ID_PATTERN.fullmatch(allow_evaluation["id"])
    assert allow_evaluation["decision"] == "allow"
    assert allow_evaluation["policy_hash"] == allow_policy["policy_hash"]
    assert allow_evaluation["quote_hash"] == quote["quote_hash"]
    assert allow_evaluation["evaluation_version"] == "1"
    assert allow_evaluation["checks"]
    assert (
        domain_client.get(f"/api/v1/policy-evaluations/{allow_evaluation['id']}").json()
        == allow_evaluation
    )

    assert deny_evaluation["decision"] == "deny"
    assert "DENY_AMOUNT_EXCEEDS_LIMIT" in deny_evaluation["reason_codes"]
    assert any(
        check["rule"] == "MAXIMUM_AMOUNT" and check["result"] == "fail"
        for check in deny_evaluation["checks"]
    )


def test_merchant_create_get_update_and_unknown_response(domain_client: TestClient) -> None:
    merchant = create_merchant(domain_client, "merchant-crud")

    assert ID_PATTERN.fullmatch(merchant["id"])
    assert merchant["status"] == "active"
    assert domain_client.get(f"/api/v1/merchants/{merchant['id']}").json() == merchant

    updated = domain_client.patch(
        f"/api/v1/merchants/{merchant['id']}",
        json={"name": "Updated Merchant", "status": "suspended"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated Merchant"
    assert updated.json()["status"] == "suspended"

    missing = domain_client.get("/api/v1/merchants/mrc_00000000000000000000000000")
    assert missing.status_code == 404


def test_duplicate_merchant_slug_is_a_conflict(domain_client: TestClient) -> None:
    payload = merchant_payload("duplicate-merchant")
    assert domain_client.post("/api/v1/merchants", json=payload).status_code == 201
    duplicate = domain_client.post("/api/v1/merchants", json=payload)

    assert duplicate.status_code == 409
    assert "slug" in duplicate.json()["detail"].lower()


def test_service_crud_uses_integer_money_and_round_trips_schemas(
    domain_client: TestClient,
) -> None:
    merchant = create_merchant(domain_client, "service-crud-merchant")
    payload = service_payload("service-crud", status="draft", base_price=500)
    created_response = domain_client.post(
        f"/api/v1/merchants/{merchant['id']}/services",
        json=payload,
    )
    assert created_response.status_code == 201
    created = created_response.json()

    assert ID_PATTERN.fullmatch(created["id"])
    assert created["base_price"] == 500
    assert isinstance(created["base_price"], int)
    assert created["input_schema"] == payload["input_schema"]
    assert created["output_schema"] == payload["output_schema"]
    assert domain_client.get(f"/api/v1/services/{created['id']}").json() == created

    updated_response = domain_client.patch(
        f"/api/v1/services/{created['id']}",
        json={
            "status": "active",
            "base_price": 700,
            "output_schema": {"type": "object", "required": ["result"]},
        },
    )
    assert updated_response.status_code == 200
    updated = updated_response.json()
    assert updated["status"] == "active"
    assert updated["base_price"] == 700
    assert updated["output_schema"] == {"type": "object", "required": ["result"]}


def test_service_slug_scope_and_unknown_merchant(domain_client: TestClient) -> None:
    first_merchant = create_merchant(domain_client, "slug-scope-first")
    second_merchant = create_merchant(domain_client, "slug-scope-second")
    payload = service_payload("shared-service-slug")

    first = domain_client.post(
        f"/api/v1/merchants/{first_merchant['id']}/services",
        json=payload,
    )
    second = domain_client.post(
        f"/api/v1/merchants/{second_merchant['id']}/services",
        json=payload,
    )
    duplicate = domain_client.post(
        f"/api/v1/merchants/{first_merchant['id']}/services",
        json=payload,
    )
    missing_merchant = domain_client.post(
        "/api/v1/merchants/mrc_00000000000000000000000000/services",
        json=payload,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert duplicate.status_code == 409
    assert missing_merchant.status_code == 404


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_price", -1),
        ("base_price", 5.5),
        ("base_price", (1 << 53)),
        ("status", "unknown"),
        ("currency", "inr"),
        ("input_schema", []),
        ("maximum_fulfillment_seconds", 0),
        ("unexpected", "field"),
    ],
)
def test_service_invalid_inputs_return_422(
    domain_client: TestClient,
    field: str,
    value: Any,
) -> None:
    merchants = domain_client.get("/api/v1/merchants").json()
    merchant = next(
        (item for item in merchants if item["slug"] == "validation-merchant"),
        None,
    )
    if merchant is None:
        merchant = create_merchant(domain_client, "validation-merchant")

    payload = service_payload("invalid-service")
    payload[field] = value
    response = domain_client.post(
        f"/api/v1/merchants/{merchant['id']}/services",
        json=payload,
    )

    assert response.status_code == 422


def test_catalog_filters_lifecycle_and_exposes_machine_contract(
    domain_client: TestClient,
) -> None:
    active_merchant = create_merchant(domain_client, "catalog-active-merchant")
    inactive_merchant = create_merchant(
        domain_client,
        "catalog-inactive-merchant",
        status="inactive",
    )
    visible = create_service(
        domain_client,
        active_merchant["id"],
        "catalog-visible-service",
        status="active",
        base_price=725,
    )
    hidden_service = create_service(
        domain_client,
        active_merchant["id"],
        "catalog-hidden-service",
        status="inactive",
    )
    hidden_merchant_service = create_service(
        domain_client,
        inactive_merchant["id"],
        "catalog-hidden-merchant-service",
        status="active",
    )

    response = domain_client.get("/api/v1/catalog")
    assert response.status_code == 200
    catalog = response.json()
    merchant_slugs = [merchant["slug"] for merchant in catalog["merchants"]]
    service_rows = {
        service["id"]: (merchant, service)
        for merchant in catalog["merchants"]
        for service in merchant["services"]
    }

    assert catalog["version"] == "1"
    assert merchant_slugs == sorted(merchant_slugs)
    assert all(
        [service["slug"] for service in merchant["services"]]
        == sorted(service["slug"] for service in merchant["services"])
        for merchant in catalog["merchants"]
    )
    assert visible["id"] in service_rows
    assert hidden_service["id"] not in service_rows
    assert hidden_merchant_service["id"] not in service_rows

    catalog_merchant, catalog_service = service_rows[visible["id"]]
    assert catalog_merchant["id"] == active_merchant["id"]
    assert catalog_service["status"] == "active"
    assert catalog_service["pricing"] == {"amount": 725, "currency": "INR"}
    assert catalog_service["input_schema"] == service_payload("unused")["input_schema"]
    assert catalog_service["output_schema"] == service_payload("unused")["output_schema"]
    assert catalog_service["maximum_fulfillment_seconds"] == 30
    assert catalog_service["refund_on_fulfillment_failure"] is True
    assert "base_price" not in catalog_service
    assert "created_at" not in catalog_service

    detail = domain_client.get(f"/api/v1/catalog/services/{visible['id']}")
    assert detail.status_code == 200
    assert detail.json()["merchant"]["id"] == active_merchant["id"]
    assert detail.json()["service"] == catalog_service
    assert domain_client.get(f"/api/v1/catalog/services/{hidden_service['id']}").status_code == 404
    assert (
        domain_client.get(f"/api/v1/catalog/services/{hidden_merchant_service['id']}").status_code
        == 404
    )


def test_quote_creation_is_server_authoritative_fresh_and_lookup_is_immutable(
    domain_client: TestClient,
) -> None:
    merchant = create_merchant(domain_client, "quote-authority-merchant")
    service = create_service(
        domain_client,
        merchant["id"],
        "quote-authority-service",
        base_price=725,
    )
    request = {"service_id": service["id"], "input": {"norad_id": 25544}}

    first_response = domain_client.post("/api/v1/quotes", json=request)
    second_response = domain_client.post("/api/v1/quotes", json=request)

    assert first_response.status_code == 201, first_response.text
    assert second_response.status_code == 201, second_response.text
    first = first_response.json()
    second = second_response.json()
    assert ID_PATTERN.fullmatch(first["id"])
    assert first["merchant"] == {
        "id": merchant["id"],
        "slug": merchant["slug"],
        "name": merchant["name"],
    }
    assert first["service"]["id"] == service["id"]
    assert first["service"]["name"] == service["name"]
    assert first["input"] == request["input"]
    assert first["input_hash"].startswith("sha256:")
    assert first["pricing"] == {
        "amount": 725,
        "currency": "INR",
        "purchase_type": "one_time",
    }
    assert first["fulfillment"] == {
        "maximum_seconds": 30,
        "refund_on_failure": True,
    }
    assert first["state"] == "active"
    assert first["quote_hash"].startswith("sha256:")
    assert second["id"] != first["id"]
    assert second["quote_hash"] != first["quote_hash"]
    assert second["input_hash"] == first["input_hash"]
    assert domain_client.get(f"/api/v1/quotes/{first['id']}").json() == first

    forbidden_terms = domain_client.post(
        "/api/v1/quotes",
        json={**request, "amount": 1, "currency": "USD"},
    )
    assert forbidden_terms.status_code == 422
    assert domain_client.patch(f"/api/v1/quotes/{first['id']}", json={}).status_code == 405


@pytest.mark.parametrize(
    "invalid_input",
    [
        {},
        {"norad_id": "ISS"},
        {"norad_id": 25544, "unexpected": "value"},
    ],
)
def test_quote_input_is_validated_against_the_persisted_schema(
    domain_client: TestClient,
    invalid_input: Any,
) -> None:
    merchants = domain_client.get("/api/v1/merchants").json()
    merchant = next(
        (item for item in merchants if item["slug"] == "quote-validation-merchant"),
        None,
    )
    if merchant is None:
        merchant = create_merchant(domain_client, "quote-validation-merchant")
    services = domain_client.get(f"/api/v1/merchants/{merchant['id']}/services").json()
    service = next(
        (item for item in services if item["slug"] == "quote-validation-service"),
        None,
    )
    if service is None:
        service = create_service(
            domain_client,
            merchant["id"],
            "quote-validation-service",
        )

    response = domain_client.post(
        "/api/v1/quotes",
        json={"service_id": service["id"], "input": invalid_input},
    )

    assert response.status_code == 422
    body = response.json()
    assert "Traceback" not in str(body)
    assert "SELECT " not in str(body)


def test_quote_eligibility_distinguishes_unknown_and_inactive_records(
    domain_client: TestClient,
) -> None:
    active_merchant = create_merchant(domain_client, "quote-gating-active-merchant")
    inactive_merchant = create_merchant(
        domain_client,
        "quote-gating-inactive-merchant",
        status="inactive",
    )
    inactive_service = create_service(
        domain_client,
        active_merchant["id"],
        "quote-gating-inactive-service",
        status="inactive",
    )
    inactive_merchant_service = create_service(
        domain_client,
        inactive_merchant["id"],
        "quote-gating-inactive-merchant-service",
    )
    request_input = {"norad_id": 25544}

    assert (
        domain_client.post(
            "/api/v1/quotes",
            json={"service_id": "svc_00000000000000000000000000", "input": request_input},
        ).status_code
        == 404
    )
    assert (
        domain_client.post(
            "/api/v1/quotes",
            json={"service_id": inactive_service["id"], "input": request_input},
        ).status_code
        == 409
    )
    assert (
        domain_client.post(
            "/api/v1/quotes",
            json={"service_id": inactive_merchant_service["id"], "input": request_input},
        ).status_code
        == 409
    )


def test_quote_snapshot_and_row_survive_service_edits_and_database_mutation_attempts(
    domain_client: TestClient,
    isolated_database: IsolatedDatabase,
) -> None:
    merchant = create_merchant(domain_client, "quote-history-merchant")
    service = create_service(
        domain_client,
        merchant["id"],
        "quote-history-service",
        base_price=500,
    )
    created_response = domain_client.post(
        "/api/v1/quotes",
        json={"service_id": service["id"], "input": {"norad_id": 25544}},
    )
    assert created_response.status_code == 201, created_response.text
    original = created_response.json()

    changed_service = domain_client.patch(
        f"/api/v1/services/{service['id']}",
        json={
            "name": "Changed Service Name",
            "base_price": 999,
            "maximum_fulfillment_seconds": 90,
            "refund_on_fulfillment_failure": False,
        },
    )
    assert changed_service.status_code == 200, changed_service.text
    assert domain_client.get(f"/api/v1/quotes/{original['id']}").json() == original

    async def attempt_raw_mutation(statement: str) -> None:
        async with isolated_database.database.engine.begin() as connection:
            await connection.execute(text(statement), {"quote_id": original["id"]})

    with pytest.raises(DBAPIError):
        asyncio.run(
            attempt_raw_mutation("UPDATE quotes SET amount = amount + 1 WHERE id = :quote_id")
        )
    with pytest.raises(DBAPIError):
        asyncio.run(attempt_raw_mutation("DELETE FROM quotes WHERE id = :quote_id"))

    async def attempt_service_delete() -> None:
        async with isolated_database.database.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM services WHERE id = :service_id"),
                {"service_id": service["id"]},
            )

    with pytest.raises(DBAPIError):
        asyncio.run(attempt_service_delete())

    assert domain_client.get(f"/api/v1/quotes/{original['id']}").json() == original
    assert domain_client.get(f"/api/v1/services/{service['id']}").status_code == 200


def test_quote_input_supports_non_object_json_when_the_service_schema_allows_it(
    domain_client: TestClient,
) -> None:
    merchant = create_merchant(domain_client, "quote-json-value-merchant")
    service = create_service(
        domain_client,
        merchant["id"],
        "quote-json-value-service",
        input_schema={"type": "null"},
    )

    response = domain_client.post(
        "/api/v1/quotes",
        json={"service_id": service["id"], "input": None},
    )

    assert response.status_code == 201, response.text
    quote = response.json()
    assert quote["input"] is None
    assert domain_client.get(f"/api/v1/quotes/{quote['id']}").json()["input"] is None
    assert domain_client.get("/api/v1/quotes/qte_00000000000000000000000000").status_code == 404


def test_development_seed_is_idempotent_and_database_backed(
    domain_client: TestClient,
    isolated_database: IsolatedDatabase,
) -> None:
    first_result = asyncio.run(seed_development_data(isolated_database.database))
    first_merchant = next(
        item
        for item in domain_client.get("/api/v1/merchants").json()
        if item["slug"] == "orbitintel"
    )
    first_services = domain_client.get(f"/api/v1/merchants/{first_merchant['id']}/services").json()

    second_result = asyncio.run(seed_development_data(isolated_database.database))
    second_merchant = next(
        item
        for item in domain_client.get("/api/v1/merchants").json()
        if item["slug"] == "orbitintel"
    )
    second_services = domain_client.get(
        f"/api/v1/merchants/{second_merchant['id']}/services"
    ).json()

    assert first_result.merchant_created is True
    assert first_result.services_created == 3
    assert second_result.merchant_created is False
    assert second_result.merchant_updated is False
    assert second_result.services_created == 0
    assert second_result.services_updated == 0
    assert second_result.services_unchanged == 3
    assert second_merchant["id"] == first_merchant["id"]
    assert second_merchant["updated_at"] == first_merchant["updated_at"]
    assert {service["id"] for service in second_services} == {
        service["id"] for service in first_services
    }
    assert {service["name"]: service["base_price"] for service in second_services} == {
        "Satellite Status Lookup": 200,
        "Orbital Risk Report": 500,
        "Detailed Orbital Analysis": 900,
    }
    assert all(service["currency"] == "INR" for service in second_services)
    assert all(service["status"] == "active" for service in second_services)
