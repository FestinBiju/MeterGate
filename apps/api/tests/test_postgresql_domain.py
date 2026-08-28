import asyncio
import json
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.api.v1.dependencies import (
    require_authenticated_mutation,
    require_current_account,
)
from app.application import create_app
from app.core.config import Settings, get_settings
from app.db.session import Database, make_async_database_url
from app.domain.approval_hashing import AUTHORIZATION_VERSION, calculate_authorization_hash
from app.domain.entitlement_hashing import calculate_entitlement_hash
from app.domain.enums import (
    AccountStatus,
    ApprovalIdentityStatus,
    FulfillmentEventActorType,
    FulfillmentEventType,
    FulfillmentExecutionState,
    FulfillmentProviderType,
    PaymentAttemptStatus,
    PaymentEventActorType,
    PaymentProvider,
    PaymentTransactionEventType,
    PaymentTransactionState,
    PurchaseType,
    RazorpayOrderStatus,
    WebhookProcessingStatus,
)
from app.domain.hashing import sha256_bytes
from app.domain.ids import (
    new_account_id,
    new_approval_identity_id,
    new_authorization_id,
    new_entitlement_id,
    new_fulfillment_event_id,
    new_fulfillment_execution_id,
    new_merchant_id,
    new_passkey_credential_id,
    new_payment_attempt_id,
    new_payment_transaction_event_id,
    new_payment_transaction_id,
    new_policy_evaluation_id,
    new_policy_id,
    new_quote_id,
    new_razorpay_webhook_event_id,
    new_service_fulfillment_config_id,
    new_service_id,
)
from app.domain.payment_hashing import (
    PAYMENT_BINDING_VERSION,
    calculate_payment_binding_hash,
)
from app.models import (
    Account,
    ApprovalIdentity,
    Entitlement,
    FulfillmentEvent,
    FulfillmentExecution,
    PasskeyCredential,
    PaymentAttempt,
    PaymentTransaction,
    PaymentTransactionEvent,
    PurchaseAuthorization,
    Quote,
    RazorpayWebhookEvent,
    ServiceFulfillmentConfig,
)
from app.providers.fulfillment import MerchantFulfillmentResult
from app.repositories.accounts import AccountRepository
from app.repositories.approval_identities import ApprovalIdentityRepository
from app.repositories.authorizations import PurchaseAuthorizationRepository
from app.repositories.entitlements import EntitlementRepository
from app.repositories.passkey_credentials import PasskeyCredentialRepository
from app.repositories.payment_attempts import PaymentAttemptRepository
from app.repositories.payment_transactions import PaymentTransactionRepository
from app.repositories.service_fulfillment_configs import ServiceFulfillmentConfigRepository
from app.scripts.seed_dev import seed_development_data
from app.services.capabilities import CapabilityTokenService
from app.services.fulfillments import FulfillmentApplicationService
from app.services.payments import PaymentApplicationService
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


@dataclass(frozen=True, slots=True)
class AuthenticatedAccountContext:
    account: Account
    approval_identity: ApprovalIdentity


_MISSING_OVERRIDE = object()


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


def _alembic_downgrade_to_trusted_approval(connection: Any) -> None:
    config = Config(API_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    command.downgrade(config, "20260825_0004")


def _alembic_downgrade_to_authenticated_accounts(connection: Any) -> None:
    config = Config(API_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    command.downgrade(config, "20260825_0005")


def _alembic_downgrade_to_value_release(connection: Any) -> None:
    config = Config(API_ROOT / "alembic.ini")
    config.attributes["connection"] = connection
    command.downgrade(config, "20260826_0007")


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


async def create_authenticated_account(
    isolated_database: IsolatedDatabase,
    *,
    display_name: str,
    raw_credential_id: bytes,
) -> tuple[AuthenticatedAccountContext, PasskeyCredential]:
    account = Account(
        id=new_account_id(),
        display_name=display_name,
        status=AccountStatus.ACTIVE,
        session_version=1,
    )
    identity = ApprovalIdentity(
        id=new_approval_identity_id(),
        account_id=account.id,
        subject_ref=account.id,
        display_name=display_name,
        webauthn_user_handle=uuid.uuid4().bytes + uuid.uuid4().bytes,
        status=ApprovalIdentityStatus.ACTIVE,
    )
    credential = PasskeyCredential(
        id=new_passkey_credential_id(),
        approval_identity_id=identity.id,
        credential_id=raw_credential_id,
        public_key=b"authenticated-test-public-key",
        sign_count=0,
        transports=["internal"],
    )
    async with isolated_database.database.session() as session:
        await AccountRepository(session).create_signup_bundle(
            account,
            identity=identity,
            credential=credential,
        )
    return AuthenticatedAccountContext(account, identity), credential


@contextmanager
def authenticated_domain_client(
    client: TestClient,
    current: AuthenticatedAccountContext,
) -> Iterator[TestClient]:
    overrides = client.app.dependency_overrides
    dependencies = (require_current_account, require_authenticated_mutation)
    previous = {
        dependency: overrides.get(dependency, _MISSING_OVERRIDE) for dependency in dependencies
    }

    async def resolve_current_account() -> AuthenticatedAccountContext:
        return current

    for dependency in dependencies:
        overrides[dependency] = resolve_current_account
    try:
        yield client
    finally:
        for dependency, override in previous.items():
            if override is _MISSING_OVERRIDE:
                overrides.pop(dependency, None)
            else:
                overrides[dependency] = override


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
        "accounts",
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
                    "identity_foreign_keys": {
                        item["referred_table"]: item["options"].get("ondelete")
                        for item in inspector.get_foreign_keys("approval_identities")
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
                          'accounts',
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
        "account_id": "VARCHAR(31)",
        "subject_ref": "VARCHAR(200)",
        "display_name": "VARCHAR(200)",
        "webauthn_user_handle": "BYTEA",
        "status": "VARCHAR(8)",
        "created_at": "TIMESTAMP",
        "updated_at": "TIMESTAMP",
    }
    assert {
        "ck_approval_identities_account_id_format",
        "ck_approval_identities_account_id_length_prefix",
        "ck_approval_identities_id_format",
        "ck_approval_identities_status",
        "ck_approval_identities_subject_ref_valid",
        "ck_approval_identities_webauthn_user_handle_length",
    } <= schema["identity_checks"]
    assert schema["identity_indexes"] >= {"ix_approval_identities_status_created_at"}
    assert schema["identity_unique"] == {
        "uq_approval_identities_account_id",
        "uq_approval_identities_subject_ref",
        "uq_approval_identities_webauthn_user_handle",
    }
    assert schema["identity_foreign_keys"] == {"accounts": "RESTRICT"}

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
        ("accounts", "trg_accounts_guard_mutation"),
        ("approval_identities", "trg_approval_identities_canonical_subject"),
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


def test_account_migration_has_postgresql_integrity_guards(
    isolated_database: IsolatedDatabase,
) -> None:
    async def inspect_account_tables() -> dict[str, Any]:
        async with isolated_database.database.engine.connect() as connection:

            def inspect_connection(sync_connection: Any) -> dict[str, Any]:
                inspector = inspect(sync_connection)
                identity_account_column = next(
                    column
                    for column in inspector.get_columns("approval_identities")
                    if column["name"] == "account_id"
                )
                return {
                    "account_columns": {
                        column["name"]: str(column["type"])
                        for column in inspector.get_columns("accounts")
                    },
                    "account_checks": {
                        item["name"] for item in inspector.get_check_constraints("accounts")
                    },
                    "account_indexes": {item["name"] for item in inspector.get_indexes("accounts")},
                    "identity_account_nullable": identity_account_column["nullable"],
                    "identity_foreign_keys": {
                        item["name"]: (
                            item["referred_table"],
                            item["options"].get("ondelete"),
                        )
                        for item in inspector.get_foreign_keys("approval_identities")
                    },
                    "identity_unique": {
                        item["name"]
                        for item in inspector.get_unique_constraints("approval_identities")
                    },
                }

            inspected = await connection.run_sync(inspect_connection)
            trigger_rows = await connection.execute(
                text(
                    """
                    SELECT event_object_table, trigger_name
                    FROM information_schema.triggers
                    WHERE event_object_schema = current_schema()
                      AND event_object_table IN ('accounts', 'approval_identities')
                    """
                )
            )
            inspected["triggers"] = set(trigger_rows.tuples().all())
            return inspected

    schema = asyncio.run(inspect_account_tables())

    assert schema["account_columns"] == {
        "id": "VARCHAR(31)",
        "display_name": "VARCHAR(200)",
        "status": "VARCHAR(8)",
        "session_version": "INTEGER",
        "created_at": "TIMESTAMP",
        "updated_at": "TIMESTAMP",
    }
    assert {
        "ck_accounts_display_name_valid",
        "ck_accounts_id_format",
        "ck_accounts_id_length_prefix",
        "ck_accounts_session_version_positive",
        "ck_accounts_status",
        "ck_accounts_updated_at_valid",
    } <= schema["account_checks"]
    assert schema["account_indexes"] >= {"ix_accounts_status_created_at"}
    assert schema["identity_account_nullable"] is False
    assert schema["identity_foreign_keys"] == {
        "fk_approval_identities_account": ("accounts", "RESTRICT")
    }
    assert "uq_approval_identities_account_id" in schema["identity_unique"]
    assert schema["triggers"] == {
        ("accounts", "trg_accounts_guard_mutation"),
        ("approval_identities", "trg_approval_identities_canonical_subject"),
        ("approval_identities", "trg_approval_identities_guard_mutation"),
    }


def test_payment_migration_has_postgresql_constraints_and_guards(
    isolated_database: IsolatedDatabase,
) -> None:
    async def inspect_payment_tables() -> dict[str, Any]:
        async with isolated_database.database.engine.connect() as connection:

            def inspect_connection(sync_connection: Any) -> dict[str, Any]:
                inspector = inspect(sync_connection)
                return {
                    "transaction_columns": {
                        column["name"]: str(column["type"])
                        for column in inspector.get_columns("payment_transactions")
                    },
                    "transaction_checks": {
                        item["name"]
                        for item in inspector.get_check_constraints("payment_transactions")
                    },
                    "transaction_unique": {
                        item["name"]
                        for item in inspector.get_unique_constraints("payment_transactions")
                    },
                    "transaction_foreign_keys": {
                        item["referred_table"]: item["options"].get("ondelete")
                        for item in inspector.get_foreign_keys("payment_transactions")
                    },
                    "attempt_indexes": {
                        item["name"] for item in inspector.get_indexes("payment_attempts")
                    },
                    "attempt_foreign_keys": {
                        item["name"]: (
                            item["referred_table"],
                            tuple(item["constrained_columns"]),
                            item["options"].get("ondelete"),
                        )
                        for item in inspector.get_foreign_keys("payment_attempts")
                    },
                    "webhook_unique": {
                        item["name"]
                        for item in inspector.get_unique_constraints("razorpay_webhook_events")
                    },
                    "event_unique": {
                        item["name"]
                        for item in inspector.get_unique_constraints("payment_transaction_events")
                    },
                    "event_checks": {
                        item["name"]
                        for item in inspector.get_check_constraints("payment_transaction_events")
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
                          'payment_transactions',
                          'payment_attempts',
                          'razorpay_webhook_events',
                          'payment_transaction_events'
                      )
                    """
                )
            )
            inspected["triggers"] = set(trigger_rows.tuples().all())
            functions = await connection.scalars(
                text(
                    """
                    SELECT pg_proc.proname
                    FROM pg_proc
                    JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
                    WHERE pg_namespace.nspname = current_schema()
                      AND pg_proc.proname IN (
                          'metergate_guard_payment_attempt_mutation',
                          'metergate_guard_payment_transaction_mutation',
                          'metergate_reject_payment_transaction_event_mutation',
                          'metergate_reject_razorpay_webhook_event_mutation',
                          'metergate_require_payment_transaction_audit_event'
                      )
                    """
                )
            )
            inspected["functions"] = set(functions.all())
            await connection.run_sync(_alembic_check)
            return inspected

    schema = asyncio.run(inspect_payment_tables())

    assert schema["transaction_columns"]["amount"] == "BIGINT"
    assert schema["transaction_columns"]["provider_receipt"] == "VARCHAR(40)"
    assert schema["transaction_columns"]["payment_binding_hash"] == "VARCHAR(71)"
    assert {
        "ck_payment_transactions_amount_safe_integer_range",
        "ck_payment_transactions_authorization_hash_format",
        "ck_payment_transactions_binding_version",
        "ck_payment_transactions_payment_binding_hash_format",
        "ck_payment_transactions_provider_order_binding_complete",
        "ck_payment_transactions_provider_receipt_is_id",
        "ck_payment_transactions_state_requires_provider_order",
        "ck_payment_transactions_transaction_state",
    } <= schema["transaction_checks"]
    assert schema["transaction_unique"] == {
        "uq_payment_transactions_authorization_id",
        "uq_payment_transactions_id_provider_order_id",
        "uq_payment_transactions_payment_binding_hash",
        "uq_payment_transactions_provider_order_id",
        "uq_payment_transactions_provider_receipt",
    }
    assert schema["transaction_foreign_keys"] == {
        "accounts": "RESTRICT",
        "buyer_policies": "RESTRICT",
        "merchants": "RESTRICT",
        "policy_evaluations": "RESTRICT",
        "purchase_authorizations": "RESTRICT",
        "quotes": "RESTRICT",
        "services": "RESTRICT",
    }
    assert schema["attempt_indexes"] >= {
        "ix_payment_attempts_transaction_first_seen_at",
        "uq_payment_attempts_one_captured_per_transaction",
    }
    assert schema["attempt_foreign_keys"] == {
        "fk_payment_attempts_transaction_order": (
            "payment_transactions",
            ("transaction_id", "provider_order_id"),
            "RESTRICT",
        )
    }
    assert schema["webhook_unique"] == {"uq_razorpay_webhook_events_provider_event_id"}
    assert schema["event_unique"] == {
        "uq_payment_transaction_events_idempotency_key",
        "uq_payment_transaction_events_source_webhook_event_id",
        "uq_payment_transaction_events_transaction_revision",
    }
    assert {
        "ck_payment_transaction_events_metadata_object",
        "ck_payment_transaction_events_prior_state_matches_revision",
        "ck_payment_transaction_events_reason_code_format",
    } <= schema["event_checks"]
    assert schema["triggers"] == {
        ("payment_attempts", "trg_payment_attempts_guard_mutation"),
        ("payment_transaction_events", "trg_payment_transaction_events_immutable"),
        ("payment_transactions", "trg_payment_transactions_guard_mutation"),
        ("payment_transactions", "trg_payment_transactions_paid_entitlement_outbox"),
        ("payment_transactions", "trg_payment_transactions_require_audit_event"),
        ("razorpay_webhook_events", "trg_razorpay_webhook_events_immutable"),
    }
    assert schema["functions"] >= {
        "metergate_guard_payment_attempt_mutation",
        "metergate_guard_payment_transaction_mutation",
        "metergate_reject_payment_transaction_event_mutation",
        "metergate_reject_razorpay_webhook_event_mutation",
        "metergate_require_payment_transaction_audit_event",
    }


def test_entitlement_fulfillment_migration_has_postgresql_guards(
    isolated_database: IsolatedDatabase,
) -> None:
    async def inspect_domain() -> dict[str, Any]:
        async with isolated_database.database.engine.connect() as connection:

            def inspect_connection(sync_connection: Any) -> dict[str, Any]:
                inspector = inspect(sync_connection)
                return {
                    "tables": set(inspector.get_table_names()),
                    "entitlement_columns": {
                        column["name"] for column in inspector.get_columns("entitlements")
                    },
                    "entitlement_unique": {
                        constraint["name"]
                        for constraint in inspector.get_unique_constraints("entitlements")
                    },
                    "entitlement_foreign_keys": {
                        constraint["referred_table"]: constraint["options"].get("ondelete")
                        for constraint in inspector.get_foreign_keys("entitlements")
                    },
                    "execution_unique": {
                        constraint["name"]
                        for constraint in inspector.get_unique_constraints("fulfillment_executions")
                    },
                    "execution_checks": {
                        constraint["name"]
                        for constraint in inspector.get_check_constraints("fulfillment_executions")
                    },
                }

            inspected = await connection.run_sync(inspect_connection)
            triggers = await connection.execute(
                text(
                    """
                    SELECT event_object_table, trigger_name
                    FROM information_schema.triggers
                    WHERE event_object_schema = current_schema()
                      AND event_object_table IN (
                          'commerce_outbox_events', 'entitlements',
                          'fulfillment_executions', 'fulfillment_events',
                          'service_fulfillment_configs'
                      )
                    """
                )
            )
            inspected["triggers"] = set(triggers.tuples().all())
            paid_guard_result = await connection.execute(
                text(
                    """
                    SELECT trigger.tgdeferrable, trigger.tginitdeferred,
                           pg_get_triggerdef(trigger.oid) AS definition
                    FROM pg_trigger AS trigger
                    JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
                    WHERE relation.relnamespace = current_schema()::regnamespace
                      AND relation.relname = 'payment_transactions'
                      AND trigger.tgname = 'trg_payment_transactions_paid_entitlement_outbox'
                    """
                )
            )
            paid_guard = paid_guard_result.mappings().one()
            inspected["paid_guard"] = dict(paid_guard)
            return inspected

    schema = asyncio.run(inspect_domain())

    assert {
        "commerce_outbox_events",
        "entitlements",
        "fulfillment_events",
        "fulfillment_executions",
        "service_fulfillment_configs",
    } <= schema["tables"]
    assert "updated_at" not in schema["entitlement_columns"]
    assert {
        "payment_reverification_event_id",
        "payment_reverification_revision",
        "provider_order_id",
        "provider_payment_id",
        "entitlement_hash",
    } <= schema["entitlement_columns"]
    assert "uq_entitlements_transaction_id" in schema["entitlement_unique"]
    assert set(schema["entitlement_foreign_keys"].values()) == {"RESTRICT"}
    assert "uq_fulfillment_executions_entitlement_id" in schema["execution_unique"]
    assert {
        "ck_fulfillment_executions_result_size_bounded",
        "ck_fulfillment_executions_state_payload",
    } <= schema["execution_checks"]
    assert schema["triggers"] == {
        ("commerce_outbox_events", "trg_commerce_outbox_events_guard"),
        ("entitlements", "trg_entitlements_immutable"),
        ("fulfillment_events", "trg_fulfillment_events_immutable"),
        ("fulfillment_executions", "trg_fulfillment_executions_guard"),
        ("service_fulfillment_configs", "trg_service_fulfillment_configs_guard"),
    }
    assert schema["paid_guard"]["tgdeferrable"] is True
    assert schema["paid_guard"]["tginitdeferred"] is True
    assert "DEFERRABLE INITIALLY DEFERRED" in schema["paid_guard"]["definition"]


def test_postgresql_fulfillment_terminal_evidence_is_immutable(
    isolated_database: IsolatedDatabase,
) -> None:
    async def exercise_guard() -> tuple[str, int]:
        async with isolated_database.database.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TEMP TABLE fulfillment_execution_terminal_probe
                    (LIKE fulfillment_executions INCLUDING DEFAULTS INCLUDING CONSTRAINTS)
                    ON COMMIT DROP
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TRIGGER trg_fulfillment_execution_terminal_probe
                    BEFORE UPDATE OR DELETE ON fulfillment_execution_terminal_probe
                    FOR EACH ROW EXECUTE FUNCTION metergate_guard_fulfillment_execution()
                    """
                )
            )
            started_at = datetime(2026, 8, 26, 12, tzinfo=UTC)
            terminal_at = started_at + timedelta(seconds=1)
            await connection.execute(
                text(
                    """
                    INSERT INTO fulfillment_execution_terminal_probe (
                        id, entitlement_id, account_id, transaction_id,
                        merchant_id, service_id, input_hash, execution_state,
                        attempt_count, started_at, completed_at, failed_at,
                        result_content_type, result_json, result_hash,
                        result_size_bytes, failure_code, compensation_required,
                        revision, lease_generation, lease_expires_at
                    ) VALUES (
                        :succeeded_id, :entitlement_id, :account_id, :transaction_id,
                        :merchant_id, :service_id, :input_hash, 'succeeded',
                        1, :started_at, :terminal_at, NULL,
                        'application/json', CAST(:result_json AS jsonb), :result_hash,
                        11, NULL, false, 1, 1, NULL
                    ), (
                        :permanent_id, :entitlement_id, :account_id, :transaction_id,
                        :merchant_id, :service_id, :input_hash, 'permanent_failure',
                        1, :started_at, NULL, :terminal_at,
                        NULL, NULL, NULL, NULL, 'FULFILLMENT_FAILED', true, 1, 1, NULL
                    ), (
                        :reconciliation_id, :entitlement_id, :account_id, :transaction_id,
                        :merchant_id, :service_id, :input_hash, 'reconciliation_required',
                        1, :started_at, NULL, :terminal_at,
                        NULL, NULL, NULL, NULL, 'FULFILLMENT_INTEGRITY_FAILED',
                        false, 1, 1, NULL
                    )
                    """
                ),
                {
                    "succeeded_id": "ful_00000000000000000000000000",
                    "permanent_id": "ful_11111111111111111111111111",
                    "reconciliation_id": "ful_22222222222222222222222222",
                    "entitlement_id": "ent_00000000000000000000000000",
                    "account_id": "acct_00000000000000000000000000",
                    "transaction_id": "txn_00000000000000000000000000",
                    "merchant_id": "mrc_00000000000000000000000000",
                    "service_id": "svc_00000000000000000000000000",
                    "input_hash": f"sha256:{'0' * 64}",
                    "started_at": started_at,
                    "terminal_at": terminal_at,
                    "result_json": json.dumps({"ok": True}),
                    "result_hash": f"sha256:{'1' * 64}",
                },
            )

            async def assert_rejected(statement: str, parameters: dict[str, object]) -> None:
                savepoint = await connection.begin_nested()
                try:
                    with pytest.raises(DBAPIError):
                        await connection.execute(text(statement), parameters)
                finally:
                    if savepoint.is_active:
                        await savepoint.rollback()

            succeeded_id = "ful_00000000000000000000000000"
            for assignment, value in (
                ("result_content_type = :value", "application/problem+json"),
                ("result_json = CAST(:value AS jsonb)", json.dumps({"ok": False})),
                ("result_hash = :value", f"sha256:{'2' * 64}"),
                ("result_size_bytes = :value", 12),
                ("completed_at = :value", terminal_at + timedelta(seconds=1)),
            ):
                await assert_rejected(
                    f"""
                    UPDATE fulfillment_execution_terminal_probe
                    SET {assignment}, revision = revision + 1
                    WHERE id = :execution_id
                    """,
                    {"execution_id": succeeded_id, "value": value},
                )

            await assert_rejected(
                """
                UPDATE fulfillment_execution_terminal_probe
                SET execution_state = 'executing', revision = revision + 1
                WHERE id = :execution_id
                """,
                {"execution_id": succeeded_id},
            )
            await assert_rejected(
                """
                UPDATE fulfillment_execution_terminal_probe
                SET failure_code = 'FULFILLMENT_FAILURE_REWRITTEN',
                    revision = revision + 1
                WHERE id = :execution_id
                """,
                {"execution_id": "ful_11111111111111111111111111"},
            )
            await assert_rejected(
                """
                UPDATE fulfillment_execution_terminal_probe
                SET failure_code = 'FULFILLMENT_FAILURE_REWRITTEN',
                    revision = revision + 1
                WHERE id = :execution_id
                """,
                {"execution_id": "ful_22222222222222222222222222"},
            )

            await connection.execute(
                text(
                    """
                    UPDATE fulfillment_execution_terminal_probe
                    SET execution_state = 'executing', failed_at = NULL,
                        failure_code = NULL, lease_generation = lease_generation + 1,
                        lease_expires_at = :lease_expires_at, revision = revision + 1
                    WHERE id = :execution_id
                    """
                ),
                {
                    "execution_id": "ful_22222222222222222222222222",
                    "lease_expires_at": started_at + timedelta(minutes=5),
                },
            )
            recovered = (
                await connection.execute(
                    text(
                        """
                        SELECT execution_state, revision
                        FROM fulfillment_execution_terminal_probe
                        WHERE id = :execution_id
                        """
                    ),
                    {"execution_id": "ful_22222222222222222222222222"},
                )
            ).one()
            return str(recovered.execution_state), recovered.revision

    assert asyncio.run(exercise_guard()) == (
        FulfillmentExecutionState.EXECUTING.value,
        2,
    )


def test_route_pin_migration_quarantines_ambiguous_dispatches_and_preserves_route_guard() -> None:
    """Exercise the non-empty 0007 -> 0008 upgrade against PostgreSQL itself."""

    async def exercise_upgrade() -> None:
        isolated = await _prepare_isolated_database()
        # Keep fixture writes slightly behind wall-clock migration time: 0008
        # correctly preserves the table invariant ``updated_at >= created_at``.
        now = datetime.now(UTC) - timedelta(seconds=1)
        try:
            account_id = new_account_id()
            identity_id = new_approval_identity_id()
            credential_id = new_passkey_credential_id()
            merchant_id = new_merchant_id()
            service_id = new_service_id()
            quote_id = new_quote_id()
            policy_id = new_policy_id()
            evaluation_id = new_policy_evaluation_id()
            config_id = new_service_fulfillment_config_id()
            input_hash = f"sha256:{'1' * 64}"
            policy_hash = f"sha256:{'2' * 64}"
            quote_hash = f"sha256:{'3' * 64}"
            endpoint_url = "http://127.0.0.1:8100/internal/v1/fulfillments"
            execution_ids = [new_fulfillment_execution_id() for _ in range(4)]
            entitlement_ids = [new_entitlement_id() for _ in range(4)]
            transaction_ids = [new_payment_transaction_id() for _ in range(4)]
            authorization_ids = [new_authorization_id() for _ in range(4)]
            event_ids = [new_payment_transaction_event_id() for _ in range(4)]
            attempt_ids = [new_payment_attempt_id() for _ in range(4)]

            async with isolated.database.engine.connect() as connection:
                await connection.run_sync(_alembic_downgrade_to_value_release)
                await connection.commit()

            async with isolated.database.engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO accounts (id, display_name, status, session_version)
                        VALUES (:account_id, 'Route migration fixture', 'active', 1)
                        """
                    ),
                    {"account_id": account_id},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO approval_identities (
                            id, account_id, subject_ref, display_name,
                            webauthn_user_handle, status
                        ) VALUES (
                            :identity_id, :account_id, :account_id,
                            'Route migration fixture', :user_handle, 'active'
                        )
                        """
                    ),
                    {
                        "identity_id": identity_id,
                        "account_id": account_id,
                        "user_handle": uuid.uuid4().bytes + uuid.uuid4().bytes,
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO passkey_credentials (
                            id, approval_identity_id, credential_id, public_key,
                            sign_count, transports
                        ) VALUES (
                            :credential_id, :identity_id, :credential_bytes,
                            :public_key, 0, NULL
                        )
                        """
                    ),
                    {
                        "credential_id": credential_id,
                        "identity_id": identity_id,
                        "credential_bytes": uuid.uuid4().bytes,
                        "public_key": b"route-pin-migration-test-public-key",
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO merchants (id, slug, name, description, status)
                        VALUES (
                            :merchant_id, 'route-pin-migration',
                            'Route pin migration', 'Disposable migration fixture', 'active'
                        )
                        """
                    ),
                    {"merchant_id": merchant_id},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO services (
                            id, merchant_id, slug, name, description, status,
                            service_type, purchase_type, currency, base_price,
                            input_schema, output_schema, output_content_type,
                            maximum_fulfillment_seconds, refund_on_fulfillment_failure
                        ) VALUES (
                            :service_id, :merchant_id, 'route-pin-service',
                            'Route pin service', 'Disposable migration fixture', 'active',
                            'report', 'one_time', 'INR', 500,
                            CAST(:input_schema AS jsonb), CAST(:output_schema AS jsonb),
                            'application/json', 30, true
                        )
                        """
                    ),
                    {
                        "service_id": service_id,
                        "merchant_id": merchant_id,
                        "input_schema": json.dumps({"type": "object"}),
                        "output_schema": json.dumps({"type": "object"}),
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO quotes (
                            id, merchant_id, service_id, input, input_hash,
                            service_snapshot, amount, currency, purchase_type,
                            maximum_fulfillment_seconds, refund_on_fulfillment_failure,
                            issued_at, expires_at, quote_hash
                        ) VALUES (
                            :quote_id, :merchant_id, :service_id,
                            CAST(:input_value AS jsonb), :input_hash,
                            CAST(:service_snapshot AS jsonb), 500, 'INR', 'one_time',
                            30, true, :issued_at, :expires_at, :quote_hash
                        )
                        """
                    ),
                    {
                        "quote_id": quote_id,
                        "merchant_id": merchant_id,
                        "service_id": service_id,
                        "input_value": json.dumps({"norad_id": 25544}),
                        "input_hash": input_hash,
                        "service_snapshot": json.dumps({"name": "Route pin service"}),
                        "issued_at": now,
                        "expires_at": now + timedelta(hours=1),
                        "quote_hash": quote_hash,
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO buyer_policies (
                            id, subject_ref, maximum_amount, allowed_currencies,
                            allowed_merchant_ids, allowed_service_ids,
                            allowed_service_types, allowed_purchase_types,
                            issued_at, expires_at, policy_version, policy_hash
                        ) VALUES (
                            :policy_id, :account_id, 1000, NULL, NULL, NULL, NULL, NULL,
                            :issued_at, :expires_at, '1', :policy_hash
                        )
                        """
                    ),
                    {
                        "policy_id": policy_id,
                        "account_id": account_id,
                        "issued_at": now,
                        "expires_at": now + timedelta(hours=1),
                        "policy_hash": policy_hash,
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO policy_evaluations (
                            id, policy_id, quote_id, policy_hash, quote_hash,
                            decision, checks, evaluated_at, evaluation_version
                        ) VALUES (
                            :evaluation_id, :policy_id, :quote_id, :policy_hash, :quote_hash,
                            'allow', CAST(:checks AS jsonb), :evaluated_at, '1'
                        )
                        """
                    ),
                    {
                        "evaluation_id": evaluation_id,
                        "policy_id": policy_id,
                        "quote_id": quote_id,
                        "policy_hash": policy_hash,
                        "quote_hash": quote_hash,
                        "checks": json.dumps([{"rule": "MIGRATION_FIXTURE"}]),
                        "evaluated_at": now,
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO service_fulfillment_configs (
                            id, service_id, provider_type, endpoint_url,
                            request_timeout_seconds, maximum_attempts, enabled, revision
                        ) VALUES (
                            :config_id, :service_id, 'http', :endpoint_url, 10, 3, true, 1
                        )
                        """
                    ),
                    {
                        "config_id": config_id,
                        "service_id": service_id,
                        "endpoint_url": endpoint_url,
                    },
                )

                for index, (
                    authorization_id,
                    transaction_id,
                    event_id,
                    attempt_id,
                    entitlement_id,
                ) in enumerate(
                    zip(
                        authorization_ids,
                        transaction_ids,
                        event_ids,
                        attempt_ids,
                        entitlement_ids,
                        strict=True,
                    ),
                    start=4,
                ):
                    authorization_hash = f"sha256:{index:064x}"
                    review_hash = f"sha256:{index + 10:064x}"
                    challenge_hash = f"sha256:{index + 20:064x}"
                    payment_binding_hash = f"sha256:{index + 30:064x}"
                    entitlement_hash = f"sha256:{index + 40:064x}"
                    provider_order_id = f"order_RouteMigration{index}"
                    provider_payment_id = f"pay_RouteMigration{index}"
                    await connection.execute(
                        text(
                            """
                            INSERT INTO purchase_authorizations (
                                id, approval_identity_id, passkey_credential_id,
                                evaluation_id, policy_id, policy_hash, quote_id, quote_hash,
                                merchant_id, service_id, subject_ref, amount, currency,
                                purchase_type, review_hash, challenge_hash, authorized_at,
                                expires_at, authorization_version, authorization_hash
                            ) VALUES (
                                :authorization_id, :identity_id, :credential_id,
                                :evaluation_id, :policy_id, :policy_hash, :quote_id, :quote_hash,
                                :merchant_id, :service_id, :account_id, 500, 'INR',
                                'one_time', :review_hash, :challenge_hash, :authorized_at,
                                :expires_at, '1', :authorization_hash
                            )
                            """
                        ),
                        {
                            "authorization_id": authorization_id,
                            "identity_id": identity_id,
                            "credential_id": credential_id,
                            "evaluation_id": evaluation_id,
                            "policy_id": policy_id,
                            "policy_hash": policy_hash,
                            "quote_id": quote_id,
                            "quote_hash": quote_hash,
                            "merchant_id": merchant_id,
                            "service_id": service_id,
                            "account_id": account_id,
                            "review_hash": review_hash,
                            "challenge_hash": challenge_hash,
                            "authorized_at": now,
                            "expires_at": now + timedelta(hours=1),
                            "authorization_hash": authorization_hash,
                        },
                    )
                    await connection.execute(
                        text(
                            """
                            INSERT INTO payment_transactions (
                                id, account_id, authorization_id, authorization_hash,
                                evaluation_id, policy_id, policy_hash, quote_id, quote_hash,
                                merchant_id, service_id, amount, currency, purchase_type,
                                provider, provider_receipt, provider_order_id,
                                provider_order_status, transaction_state,
                                order_creation_attempts, order_creation_started_at,
                                order_created_at, paid_at, last_reconciled_at,
                                payment_binding_version, payment_binding_hash, revision
                            ) VALUES (
                                :transaction_id, :account_id, :authorization_id,
                                :authorization_hash, :evaluation_id, :policy_id, :policy_hash,
                                :quote_id, :quote_hash, :merchant_id, :service_id, 500, 'INR',
                                'one_time', 'razorpay', :transaction_id, :provider_order_id,
                                'created', 'order_created', 1, :observed_at, :observed_at,
                                NULL, :observed_at, '1', :payment_binding_hash, 1
                            )
                            """
                        ),
                        {
                            "transaction_id": transaction_id,
                            "account_id": account_id,
                            "authorization_id": authorization_id,
                            "authorization_hash": authorization_hash,
                            "evaluation_id": evaluation_id,
                            "policy_id": policy_id,
                            "policy_hash": policy_hash,
                            "quote_id": quote_id,
                            "quote_hash": quote_hash,
                            "merchant_id": merchant_id,
                            "service_id": service_id,
                            "provider_order_id": provider_order_id,
                            "observed_at": now,
                            "payment_binding_hash": payment_binding_hash,
                        },
                    )
                    await connection.execute(
                        text(
                            """
                            INSERT INTO payment_transaction_events (
                                id, transaction_id, transaction_revision, event_type,
                                actor_type, actor_id, prior_state, resulting_state,
                                reason_code, metadata, payment_attempt_id,
                                source_webhook_event_id, idempotency_key, occurred_at
                            ) VALUES (
                                :event_id, :transaction_id, 1,
                                'payment_transaction_created', 'system', NULL, NULL,
                                'order_created', 'PAYMENT_TRANSACTION_CREATED',
                                CAST(:metadata AS jsonb), NULL, NULL,
                                :idempotency_key, :occurred_at
                            )
                            """
                        ),
                        {
                            "event_id": event_id,
                            "transaction_id": transaction_id,
                            "metadata": json.dumps({"migration_fixture": True}),
                            "idempotency_key": f"{transaction_id}:created",
                            "occurred_at": now,
                        },
                    )
                    await connection.execute(
                        text(
                            """
                            INSERT INTO payment_attempts (
                                id, transaction_id, provider, provider_order_id,
                                provider_payment_id, amount, currency, provider_status,
                                method, captured, provider_created_at, first_seen_at, last_seen_at
                            ) VALUES (
                                :attempt_id, :transaction_id, 'razorpay', :provider_order_id,
                                :provider_payment_id, 500, 'INR', 'created', NULL, false,
                                :observed_at, :observed_at, :observed_at
                            )
                            """
                        ),
                        {
                            "attempt_id": attempt_id,
                            "transaction_id": transaction_id,
                            "provider_order_id": provider_order_id,
                            "provider_payment_id": provider_payment_id,
                            "observed_at": now,
                        },
                    )
                    await connection.execute(
                        text(
                            """
                            INSERT INTO entitlements (
                                id, account_id, transaction_id, payment_binding_hash,
                                payment_reverification_event_id,
                                payment_reverification_revision, provider_order_id,
                                provider_payment_id, authorization_id, authorization_hash,
                                evaluation_id, policy_id, policy_hash, quote_id, quote_hash,
                                merchant_id, service_id, input, input_hash, amount, currency,
                                purchase_type, maximum_executions, issued_at, expires_at,
                                entitlement_version, entitlement_hash
                            ) VALUES (
                                :entitlement_id, :account_id, :transaction_id,
                                :payment_binding_hash, :event_id, 1, :provider_order_id,
                                :provider_payment_id, :authorization_id, :authorization_hash,
                                :evaluation_id, :policy_id, :policy_hash, :quote_id, :quote_hash,
                                :merchant_id, :service_id, CAST(:input_value AS jsonb),
                                :input_hash, 500, 'INR', 'one_time', 1, :issued_at,
                                :expires_at, '1', :entitlement_hash
                            )
                            """
                        ),
                        {
                            "entitlement_id": entitlement_id,
                            "account_id": account_id,
                            "transaction_id": transaction_id,
                            "payment_binding_hash": payment_binding_hash,
                            "event_id": event_id,
                            "provider_order_id": provider_order_id,
                            "provider_payment_id": provider_payment_id,
                            "authorization_id": authorization_id,
                            "authorization_hash": authorization_hash,
                            "evaluation_id": evaluation_id,
                            "policy_id": policy_id,
                            "policy_hash": policy_hash,
                            "quote_id": quote_id,
                            "quote_hash": quote_hash,
                            "merchant_id": merchant_id,
                            "service_id": service_id,
                            "input_value": json.dumps({"norad_id": 25544}),
                            "input_hash": input_hash,
                            "issued_at": now,
                            "expires_at": now + timedelta(hours=1),
                            "entitlement_hash": entitlement_hash,
                        },
                    )

                for execution_id, entitlement_id, transaction_id, execution_state in zip(
                    execution_ids,
                    entitlement_ids,
                    transaction_ids,
                    ("executing", "pending", "succeeded", "executing"),
                    strict=True,
                ):
                    if execution_state == "pending":
                        values = {
                            "execution_id": execution_id,
                            "entitlement_id": entitlement_id,
                            "transaction_id": transaction_id,
                            "account_id": account_id,
                            "merchant_id": merchant_id,
                            "service_id": service_id,
                            "input_hash": input_hash,
                            "execution_state": execution_state,
                            "created_at": now,
                            "updated_at": now,
                        }
                        await connection.execute(
                            text(
                                """
                                INSERT INTO fulfillment_executions (
                                    id, entitlement_id, account_id, transaction_id,
                                    merchant_id, service_id, input_hash, execution_state,
                                    attempt_count, started_at, completed_at, failed_at,
                                    result_content_type, result_json, result_hash,
                                    result_size_bytes, failure_code, compensation_required,
                                    revision, lease_generation, lease_expires_at,
                                    created_at, updated_at
                                ) VALUES (
                                    :execution_id, :entitlement_id, :account_id, :transaction_id,
                                    :merchant_id, :service_id, :input_hash, :execution_state,
                                    0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, false,
                                    1, 0, NULL, :created_at, :updated_at
                                )
                                """
                            ),
                            values,
                        )
                    elif execution_state == "executing":
                        await connection.execute(
                            text(
                                """
                                INSERT INTO fulfillment_executions (
                                    id, entitlement_id, account_id, transaction_id,
                                    merchant_id, service_id, input_hash, execution_state,
                                    attempt_count, started_at, completed_at, failed_at,
                                    result_content_type, result_json, result_hash,
                                    result_size_bytes, failure_code, compensation_required,
                                    revision, lease_generation, lease_expires_at,
                                    created_at, updated_at
                                ) VALUES (
                                    :execution_id, :entitlement_id, :account_id, :transaction_id,
                                    :merchant_id, :service_id, :input_hash, 'executing',
                                    1, :started_at, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                                    false, 7, 1, :lease_expires_at, :created_at, :updated_at
                                )
                                """
                            ),
                            {
                                "execution_id": execution_id,
                                "entitlement_id": entitlement_id,
                                "transaction_id": transaction_id,
                                "account_id": account_id,
                                "merchant_id": merchant_id,
                                "service_id": service_id,
                                "input_hash": input_hash,
                                "started_at": now,
                                "lease_expires_at": now + timedelta(minutes=5),
                                "created_at": now,
                                "updated_at": now,
                            },
                        )
                    else:
                        await connection.execute(
                            text(
                                """
                                INSERT INTO fulfillment_executions (
                                    id, entitlement_id, account_id, transaction_id,
                                    merchant_id, service_id, input_hash, execution_state,
                                    attempt_count, started_at, completed_at, failed_at,
                                    result_content_type, result_json, result_hash,
                                    result_size_bytes, failure_code, compensation_required,
                                    revision, lease_generation, lease_expires_at,
                                    created_at, updated_at
                                ) VALUES (
                                    :execution_id, :entitlement_id, :account_id, :transaction_id,
                                    :merchant_id, :service_id, :input_hash, 'succeeded',
                                    1, :started_at, :completed_at, NULL, 'application/json',
                                    CAST(:result_json AS jsonb), :result_hash, 11, NULL, false,
                                    4, 1, NULL, :created_at, :updated_at
                                )
                                """
                            ),
                            {
                                "execution_id": execution_id,
                                "entitlement_id": entitlement_id,
                                "transaction_id": transaction_id,
                                "account_id": account_id,
                                "merchant_id": merchant_id,
                                "service_id": service_id,
                                "input_hash": input_hash,
                                "started_at": now,
                                "completed_at": now + timedelta(seconds=1),
                                "result_json": json.dumps({"ok": True}),
                                "result_hash": f"sha256:{'4' * 64}",
                                "created_at": now,
                                "updated_at": now + timedelta(seconds=1),
                            },
                        )

                # One audit record is compatible with the current config and the
                # other is deliberately incomplete, proving the old dispatch is
                # ambiguous rather than safely backfillable.
                for metadata in (
                    {
                        "fulfillment_config_id": config_id,
                        "fulfillment_config_revision": 1,
                    },
                    {"fulfillment_config_id": config_id},
                ):
                    await connection.execute(
                        text(
                            """
                            INSERT INTO fulfillment_events (
                                id, transaction_id, entitlement_id, execution_id,
                                execution_revision, event_type, actor_type, actor_id,
                                reason_code, metadata, idempotency_key, occurred_at
                            ) VALUES (
                                :event_id, :transaction_id, :entitlement_id, :execution_id,
                                7, 'merchant_request_sent', 'system', NULL,
                                'FULFILLMENT_REQUEST_SENT', CAST(:metadata AS jsonb),
                                :idempotency_key, :occurred_at
                            )
                            """
                        ),
                        {
                            "event_id": new_fulfillment_event_id(),
                            "transaction_id": transaction_ids[0],
                            "entitlement_id": entitlement_ids[0],
                            "execution_id": execution_ids[0],
                            "metadata": json.dumps(metadata),
                            "idempotency_key": f"{execution_ids[0]}:{len(metadata)}",
                            "occurred_at": now,
                        },
                    )

                # Every send for this execution proves the same config revision,
                # so 0008 can safely recover the historical endpoint snapshot.
                for suffix in ("first", "retry"):
                    await connection.execute(
                        text(
                            """
                            INSERT INTO fulfillment_events (
                                id, transaction_id, entitlement_id, execution_id,
                                execution_revision, event_type, actor_type, actor_id,
                                reason_code, metadata, idempotency_key, occurred_at
                            ) VALUES (
                                :event_id, :transaction_id, :entitlement_id, :execution_id,
                                7, 'merchant_request_sent', 'system', NULL,
                                'MERCHANT_REQUEST_SENT', CAST(:metadata AS jsonb),
                                :idempotency_key, :occurred_at
                            )
                            """
                        ),
                        {
                            "event_id": new_fulfillment_event_id(),
                            "transaction_id": transaction_ids[3],
                            "entitlement_id": entitlement_ids[3],
                            "execution_id": execution_ids[3],
                            "metadata": json.dumps(
                                {
                                    "fulfillment_config_id": config_id,
                                    "fulfillment_config_revision": 1,
                                }
                            ),
                            "idempotency_key": f"{execution_ids[3]}:{suffix}",
                            "occurred_at": now,
                        },
                    )

            async with isolated.database.engine.connect() as connection:
                await connection.run_sync(_alembic_upgrade)
                await connection.commit()

            async with isolated.database.engine.begin() as connection:
                quarantined = (
                    await connection.execute(
                        text(
                            """
                            SELECT execution_state, revision, failure_code,
                                   compensation_required, lease_expires_at
                            FROM fulfillment_executions WHERE id = :execution_id
                            """
                        ),
                        {"execution_id": execution_ids[0]},
                    )
                ).one()
                assert quarantined == (
                    "permanent_failure",
                    8,
                    "FULFILLMENT_ROUTE_PROVENANCE_UNKNOWN",
                    True,
                    None,
                )
                quarantine_events = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT event_type, execution_revision, reason_code, metadata
                            FROM fulfillment_events
                            WHERE execution_id = :execution_id
                              AND execution_revision = 8
                            ORDER BY event_type
                            """
                            ),
                            {"execution_id": execution_ids[0]},
                        )
                    )
                    .mappings()
                    .all()
                )
                assert [
                    (event["event_type"], event["execution_revision"], event["reason_code"])
                    for event in quarantine_events
                ] == [
                    ("compensation_required", 8, "FULFILLMENT_COMPENSATION_REQUIRED"),
                    ("fulfillment_failed", 8, "FULFILLMENT_PERMANENT_FAILURE"),
                ]
                assert all(
                    event["metadata"]["failure_code"] == "FULFILLMENT_ROUTE_PROVENANCE_UNKNOWN"
                    for event in quarantine_events
                )

                terminal = (
                    await connection.execute(
                        text(
                            """
                            SELECT execution_state, revision, fulfillment_config_id,
                                   result_hash, compensation_required
                            FROM fulfillment_executions WHERE id = :execution_id
                            """
                        ),
                        {"execution_id": execution_ids[2]},
                    )
                ).one()
                assert terminal == ("succeeded", 4, None, f"sha256:{'4' * 64}", False)

                proven_route = (
                    await connection.execute(
                        text(
                            """
                            SELECT fulfillment_config_id, fulfillment_config_revision,
                                   provider_type, endpoint_url,
                                   request_timeout_seconds, maximum_attempts,
                                   execution_state, revision
                            FROM fulfillment_executions WHERE id = :execution_id
                            """
                        ),
                        {"execution_id": execution_ids[3]},
                    )
                ).one()
                assert proven_route == (
                    config_id,
                    1,
                    "http",
                    endpoint_url,
                    10,
                    3,
                    "executing",
                    7,
                )

                await connection.execute(
                    text(
                        """
                        UPDATE fulfillment_executions
                        SET fulfillment_config_id = :config_id,
                            fulfillment_config_revision = 1,
                            provider_type = 'http', endpoint_url = :endpoint_url,
                            request_timeout_seconds = 10, maximum_attempts = 3,
                            revision = revision + 1, updated_at = :updated_at
                        WHERE id = :execution_id
                        """
                    ),
                    {
                        "config_id": config_id,
                        "endpoint_url": endpoint_url,
                        "updated_at": now + timedelta(seconds=2),
                        "execution_id": execution_ids[1],
                    },
                )
                pinned = (
                    await connection.execute(
                        text(
                            """
                            SELECT fulfillment_config_id, fulfillment_config_revision,
                                   provider_type, endpoint_url,
                                   request_timeout_seconds, maximum_attempts, revision
                            FROM fulfillment_executions WHERE id = :execution_id
                            """
                        ),
                        {"execution_id": execution_ids[1]},
                    )
                ).one()
                assert pinned == (config_id, 1, "http", endpoint_url, 10, 3, 2)

                savepoint = await connection.begin_nested()
                try:
                    with pytest.raises(DBAPIError):
                        await connection.execute(
                            text(
                                """
                                UPDATE fulfillment_executions
                                SET endpoint_url = 'http://127.0.0.1:8999/rewritten',
                                    revision = revision + 1,
                                    updated_at = :updated_at
                                WHERE id = :execution_id
                                """
                            ),
                            {
                                "updated_at": now + timedelta(seconds=3),
                                "execution_id": execution_ids[1],
                            },
                        )
                finally:
                    if savepoint.is_active:
                        await savepoint.rollback()

            # A populated downgrade restores the 0007 trigger before dropping
            # route columns; the next upgrade safely reconstructs only the route
            # whose complete request audit still proves the current config.
            async with isolated.database.engine.connect() as connection:
                await connection.run_sync(_alembic_downgrade_to_value_release)
                await connection.commit()
                await connection.run_sync(_alembic_upgrade)
                await connection.commit()

            async with isolated.database.engine.begin() as connection:
                reupgraded = (
                    await connection.execute(
                        text(
                            """
                            SELECT fulfillment_config_id, endpoint_url,
                                   execution_state, revision
                            FROM fulfillment_executions WHERE id = :execution_id
                            """
                        ),
                        {"execution_id": execution_ids[3]},
                    )
                ).one()
                assert reupgraded == (config_id, endpoint_url, "executing", 7)
                quarantined_again = (
                    await connection.execute(
                        text(
                            """
                            SELECT execution_state, revision, compensation_required
                            FROM fulfillment_executions WHERE id = :execution_id
                            """
                        ),
                        {"execution_id": execution_ids[0]},
                    )
                ).one()
                assert quarantined_again == ("permanent_failure", 8, True)
        finally:
            await isolated.database.dispose()
            await _drop_isolated_schema(isolated.database_url, isolated.schema)

    asyncio.run(exercise_upgrade())


def test_payment_migration_downgrades_to_accounts_and_reupgrades() -> None:
    isolated = asyncio.run(_prepare_isolated_database())

    async def exercise_cycle() -> None:
        try:
            async with isolated.database.engine.begin() as connection:

                def payment_tables(sync_connection: Any) -> set[str]:
                    return set(inspect(sync_connection).get_table_names()) & {
                        "payment_attempts",
                        "payment_transaction_events",
                        "payment_transactions",
                        "razorpay_webhook_events",
                    }

                assert await connection.run_sync(payment_tables) == {
                    "payment_attempts",
                    "payment_transaction_events",
                    "payment_transactions",
                    "razorpay_webhook_events",
                }
                await connection.run_sync(_alembic_downgrade_to_authenticated_accounts)
                assert await connection.run_sync(payment_tables) == set()
                function_count = await connection.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_proc
                        JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
                        WHERE pg_namespace.nspname = current_schema()
                          AND pg_proc.proname IN (
                              'metergate_guard_payment_attempt_mutation',
                              'metergate_guard_payment_transaction_mutation',
                              'metergate_reject_payment_transaction_event_mutation',
                              'metergate_reject_razorpay_webhook_event_mutation',
                              'metergate_require_payment_transaction_audit_event'
                          )
                        """
                    )
                )
                assert function_count == 0
                await connection.run_sync(_alembic_upgrade)
                assert await connection.run_sync(payment_tables) == {
                    "payment_attempts",
                    "payment_transaction_events",
                    "payment_transactions",
                    "razorpay_webhook_events",
                }
                await connection.run_sync(_alembic_check)
        finally:
            await isolated.database.dispose()
            await _drop_isolated_schema(isolated.database_url, isolated.schema)

    asyncio.run(exercise_cycle())


def test_account_repository_creates_signup_bundle_atomically(
    isolated_database: IsolatedDatabase,
) -> None:
    raw_credential_id = f"account-signup-{uuid.uuid4().hex}".encode()

    def signup_records() -> tuple[Account, ApprovalIdentity, PasskeyCredential]:
        account = Account(
            id=new_account_id(),
            display_name="Transactional signup",
            status=AccountStatus.ACTIVE,
            session_version=1,
        )
        identity = ApprovalIdentity(
            id=new_approval_identity_id(),
            account_id=account.id,
            subject_ref=account.id,
            display_name=account.display_name,
            webauthn_user_handle=uuid.uuid4().bytes + uuid.uuid4().bytes,
            status=ApprovalIdentityStatus.ACTIVE,
        )
        credential = PasskeyCredential(
            id=new_passkey_credential_id(),
            approval_identity_id=identity.id,
            credential_id=raw_credential_id,
            public_key=b"verified-signup-public-key",
            sign_count=0,
            transports=["internal"],
        )
        return account, identity, credential

    async def exercise_repository() -> None:
        account, identity, credential = signup_records()
        async with isolated_database.database.session() as session:
            persisted = await AccountRepository(session).create_signup_bundle(
                account,
                identity=identity,
                credential=credential,
            )
            assert persisted.id == account.id

        duplicate_account, duplicate_identity, duplicate_credential = signup_records()
        with pytest.raises(IntegrityError):
            async with isolated_database.database.session() as session:
                await AccountRepository(session).create_signup_bundle(
                    duplicate_account,
                    identity=duplicate_identity,
                    credential=duplicate_credential,
                )

        async with isolated_database.database.session() as session:
            assert await session.get(Account, account.id) is not None
            assert await session.get(ApprovalIdentity, identity.id) is not None
            assert await session.get(PasskeyCredential, credential.id) is not None
            assert await session.get(Account, duplicate_account.id) is None
            assert await session.get(ApprovalIdentity, duplicate_identity.id) is None
            assert await session.get(PasskeyCredential, duplicate_credential.id) is None

    asyncio.run(exercise_repository())


def test_security_row_locks_refresh_preloaded_identity_map_state(
    isolated_database: IsolatedDatabase,
) -> None:
    async def exercise_refresh() -> None:
        current, credential = await create_authenticated_account(
            isolated_database,
            display_name="Lock Refresh Buyer",
            raw_credential_id=f"lock-refresh-{uuid.uuid4().hex}".encode(),
        )
        async with (
            isolated_database.database.session() as first_session,
            isolated_database.database.session() as second_session,
        ):
            accounts = AccountRepository(first_session)
            identities = ApprovalIdentityRepository(first_session)
            credentials = PasskeyCredentialRepository(first_session)
            assert (await accounts.get(current.account.id)).status == AccountStatus.ACTIVE
            assert (
                await identities.get(current.approval_identity.id)
            ).status == ApprovalIdentityStatus.ACTIVE
            assert (
                await credentials.get_by_credential_id(credential.credential_id)
            ).sign_count == 0

            await second_session.execute(
                text(
                    """
                    UPDATE accounts
                    SET status = 'disabled',
                        session_version = session_version + 1,
                        updated_at = now()
                    WHERE id = :account_id
                    """
                ),
                {"account_id": current.account.id},
            )
            await second_session.execute(
                text(
                    """
                    UPDATE approval_identities
                    SET status = 'disabled', updated_at = now()
                    WHERE id = :identity_id
                    """
                ),
                {"identity_id": current.approval_identity.id},
            )
            await second_session.execute(
                text(
                    """
                    UPDATE passkey_credentials
                    SET sign_count = 7, last_used_at = now()
                    WHERE id = :credential_id
                    """
                ),
                {"credential_id": credential.id},
            )
            await second_session.commit()

            locked_account = await accounts.get_for_update(current.account.id)
            locked_identity = await identities.get_for_update(current.approval_identity.id)
            locked_credential = await credentials.get_by_credential_id_for_update(
                credential.credential_id,
                identity_id=current.approval_identity.id,
            )

            assert locked_account is not None
            assert locked_account.status == AccountStatus.DISABLED
            assert locked_account.session_version == 2
            assert locked_identity is not None
            assert locked_identity.status == ApprovalIdentityStatus.DISABLED
            assert locked_credential is not None
            assert locked_credential.sign_count == 7

    asyncio.run(exercise_refresh())


def test_account_database_guards_reject_unsafe_mutations(
    isolated_database: IsolatedDatabase,
) -> None:
    account_id = new_account_id()

    async def execute(statement: str, parameters: dict[str, Any]) -> None:
        async with isolated_database.database.engine.begin() as connection:
            await connection.execute(text(statement), parameters)

    asyncio.run(
        execute(
            """
            INSERT INTO accounts (id, display_name, status, session_version)
            VALUES (:account_id, 'Guarded account', 'pending', 1)
            """,
            {"account_id": account_id},
        )
    )
    with pytest.raises(DBAPIError):
        asyncio.run(
            execute(
                "UPDATE accounts SET status = 'active' WHERE id = :account_id",
                {"account_id": account_id},
            )
        )
    asyncio.run(
        execute(
            """
            UPDATE accounts
            SET status = 'active', session_version = 2, updated_at = now()
            WHERE id = :account_id
            """,
            {"account_id": account_id},
        )
    )
    for statement in (
        "UPDATE accounts SET status = 'pending', session_version = 3 WHERE id = :account_id",
        "UPDATE accounts SET session_version = 1 WHERE id = :account_id",
        "DELETE FROM accounts WHERE id = :account_id",
    ):
        with pytest.raises(DBAPIError):
            asyncio.run(execute(statement, {"account_id": account_id}))

    with pytest.raises(DBAPIError):
        asyncio.run(
            execute(
                """
                INSERT INTO approval_identities (
                    id,
                    account_id,
                    subject_ref,
                    display_name,
                    webauthn_user_handle,
                    status
                ) VALUES (
                    :identity_id,
                    :account_id,
                    'client-chosen-subject',
                    'Invalid identity',
                    :user_handle,
                    'active'
                )
                """,
                {
                    "identity_id": new_approval_identity_id(),
                    "account_id": account_id,
                    "user_handle": uuid.uuid4().bytes + uuid.uuid4().bytes,
                },
            )
        )


def test_account_migration_backfills_and_round_trips_without_rewriting_history() -> None:
    async def exercise_cycle() -> None:
        isolated = await _prepare_isolated_database()
        legacy_rows = (
            (
                "aid_00000000000000000000000011",
                "legacy-active-subject",
                "Legacy active",
                b"a" * 32,
                "active",
            ),
            (
                "aid_00000000000000000000000012",
                "legacy-pending-subject",
                "Legacy pending",
                b"b" * 32,
                "active",
            ),
            (
                "aid_00000000000000000000000013",
                "legacy-disabled-subject",
                "Legacy disabled",
                b"c" * 32,
                "disabled",
            ),
        )
        try:
            async with isolated.database.engine.connect() as connection:
                await connection.run_sync(_alembic_downgrade_to_trusted_approval)
                for identity in legacy_rows:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO approval_identities (
                                id,
                                subject_ref,
                                display_name,
                                webauthn_user_handle,
                                status
                            ) VALUES (
                                :id,
                                :subject_ref,
                                :display_name,
                                :user_handle,
                                :status
                            )
                            """
                        ),
                        {
                            "id": identity[0],
                            "subject_ref": identity[1],
                            "display_name": identity[2],
                            "user_handle": identity[3],
                            "status": identity[4],
                        },
                    )
                for credential_id, identity_id in (
                    ("pkc_00000000000000000000000011", legacy_rows[0][0]),
                    ("pkc_00000000000000000000000013", legacy_rows[2][0]),
                ):
                    await connection.execute(
                        text(
                            """
                            INSERT INTO passkey_credentials (
                                id,
                                approval_identity_id,
                                credential_id,
                                public_key,
                                sign_count,
                                transports
                            ) VALUES (
                                :id,
                                :identity_id,
                                :credential_id,
                                :public_key,
                                0,
                                NULL
                            )
                            """
                        ),
                        {
                            "id": credential_id,
                            "identity_id": identity_id,
                            "credential_id": credential_id.encode(),
                            "public_key": b"legacy-public-key",
                        },
                    )
                await connection.execute(
                    text(
                        """
                        INSERT INTO merchants (id, slug, name, description, status)
                        VALUES (
                            'mrc_00000000000000000000000011',
                            'legacy-account-merchant',
                            'Legacy account merchant',
                            'Migration preservation fixture',
                            'active'
                        )
                        """
                    )
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO services (
                            id,
                            merchant_id,
                            slug,
                            name,
                            description,
                            status,
                            service_type,
                            purchase_type,
                            currency,
                            base_price,
                            input_schema,
                            output_schema,
                            output_content_type,
                            maximum_fulfillment_seconds,
                            refund_on_fulfillment_failure
                        ) VALUES (
                            'svc_00000000000000000000000011',
                            'mrc_00000000000000000000000011',
                            'legacy-account-service',
                            'Legacy account service',
                            'Migration preservation fixture',
                            'active',
                            'report',
                            'one_time',
                            'INR',
                            500,
                            CAST(:input_schema AS jsonb),
                            CAST(:output_schema AS jsonb),
                            'application/json',
                            30,
                            true
                        )
                        """
                    ),
                    {
                        "input_schema": json.dumps({"type": "object"}),
                        "output_schema": json.dumps({"type": "object"}),
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO quotes (
                            id,
                            merchant_id,
                            service_id,
                            input,
                            input_hash,
                            service_snapshot,
                            amount,
                            currency,
                            purchase_type,
                            maximum_fulfillment_seconds,
                            refund_on_fulfillment_failure,
                            issued_at,
                            expires_at,
                            quote_hash
                        ) VALUES (
                            'qte_00000000000000000000000011',
                            'mrc_00000000000000000000000011',
                            'svc_00000000000000000000000011',
                            CAST(:quote_input AS jsonb),
                            :input_hash,
                            CAST(:service_snapshot AS jsonb),
                            500,
                            'INR',
                            'one_time',
                            30,
                            true,
                            :issued_at,
                            :expires_at,
                            :quote_hash
                        )
                        """
                    ),
                    {
                        "quote_input": json.dumps({"norad_id": 25544}),
                        "input_hash": f"sha256:{201:064x}",
                        "service_snapshot": json.dumps({"name": "Legacy account service"}),
                        "issued_at": datetime(2026, 8, 25, 12, tzinfo=UTC),
                        "expires_at": datetime(2026, 8, 25, 13, tzinfo=UTC),
                        "quote_hash": f"sha256:{202:064x}",
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO buyer_policies (
                            id,
                            subject_ref,
                            maximum_amount,
                            allowed_currencies,
                            allowed_merchant_ids,
                            allowed_service_ids,
                            allowed_service_types,
                            allowed_purchase_types,
                            issued_at,
                            expires_at,
                            policy_version,
                            policy_hash
                        ) VALUES (
                            'pol_00000000000000000000000011',
                            :subject_ref,
                            1000,
                            NULL,
                            NULL,
                            NULL,
                            NULL,
                            NULL,
                            :issued_at,
                            :expires_at,
                            '1',
                            :policy_hash
                        )
                        """
                    ),
                    {
                        "subject_ref": legacy_rows[0][1],
                        "issued_at": datetime(2026, 8, 25, 12, tzinfo=UTC),
                        "expires_at": datetime(2026, 8, 25, 13, tzinfo=UTC),
                        "policy_hash": f"sha256:{101:064x}",
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO policy_evaluations (
                            id,
                            policy_id,
                            quote_id,
                            policy_hash,
                            quote_hash,
                            decision,
                            checks,
                            evaluated_at,
                            evaluation_version
                        ) VALUES (
                            'pye_00000000000000000000000011',
                            'pol_00000000000000000000000011',
                            'qte_00000000000000000000000011',
                            :policy_hash,
                            :quote_hash,
                            'allow',
                            CAST(:checks AS jsonb),
                            :evaluated_at,
                            '1'
                        )
                        """
                    ),
                    {
                        "policy_hash": f"sha256:{101:064x}",
                        "quote_hash": f"sha256:{202:064x}",
                        "checks": json.dumps([{"rule": "MIGRATION_FIXTURE"}]),
                        "evaluated_at": datetime(2026, 8, 25, 12, 5, tzinfo=UTC),
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO purchase_authorizations (
                            id,
                            approval_identity_id,
                            passkey_credential_id,
                            evaluation_id,
                            policy_id,
                            policy_hash,
                            quote_id,
                            quote_hash,
                            merchant_id,
                            service_id,
                            subject_ref,
                            amount,
                            currency,
                            purchase_type,
                            review_hash,
                            challenge_hash,
                            authorized_at,
                            expires_at,
                            authorization_version,
                            authorization_hash
                        ) VALUES (
                            'aut_00000000000000000000000011',
                            'aid_00000000000000000000000011',
                            'pkc_00000000000000000000000011',
                            'pye_00000000000000000000000011',
                            'pol_00000000000000000000000011',
                            :policy_hash,
                            'qte_00000000000000000000000011',
                            :quote_hash,
                            'mrc_00000000000000000000000011',
                            'svc_00000000000000000000000011',
                            :subject_ref,
                            500,
                            'INR',
                            'one_time',
                            :review_hash,
                            :challenge_hash,
                            :authorized_at,
                            :expires_at,
                            '1',
                            :authorization_hash
                        )
                        """
                    ),
                    {
                        "policy_hash": f"sha256:{101:064x}",
                        "quote_hash": f"sha256:{202:064x}",
                        "subject_ref": legacy_rows[0][1],
                        "review_hash": f"sha256:{203:064x}",
                        "challenge_hash": f"sha256:{204:064x}",
                        "authorized_at": datetime(2026, 8, 25, 12, 10, tzinfo=UTC),
                        "expires_at": datetime(2026, 8, 25, 12, 20, tzinfo=UTC),
                        "authorization_hash": f"sha256:{205:064x}",
                    },
                )
                await connection.commit()

                history_before = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT id, subject_ref
                            FROM approval_identities
                            ORDER BY id
                            """
                            )
                        )
                    )
                    .tuples()
                    .all()
                )
                policy_before = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT id, subject_ref, policy_hash
                            FROM buyer_policies
                            WHERE id = 'pol_00000000000000000000000011'
                            """
                            )
                        )
                    )
                    .tuples()
                    .one()
                )
                authorization_before = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT
                                id,
                                subject_ref,
                                policy_hash,
                                quote_hash,
                                review_hash,
                                challenge_hash,
                                authorization_hash
                            FROM purchase_authorizations
                            WHERE id = 'aut_00000000000000000000000011'
                            """
                            )
                        )
                    )
                    .tuples()
                    .one()
                )

                await connection.run_sync(_alembic_upgrade)
                backfilled = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT account.id, account.status, identity.subject_ref
                            FROM accounts AS account
                            JOIN approval_identities AS identity
                              ON identity.account_id = account.id
                            ORDER BY account.id
                            """
                            )
                        )
                    )
                    .tuples()
                    .all()
                )
                assert backfilled == [
                    (
                        "acct_00000000000000000000000011",
                        "active",
                        "legacy-active-subject",
                    ),
                    (
                        "acct_00000000000000000000000012",
                        "pending",
                        "legacy-pending-subject",
                    ),
                    (
                        "acct_00000000000000000000000013",
                        "disabled",
                        "legacy-disabled-subject",
                    ),
                ]
                assert (
                    await connection.execute(
                        text("SELECT id, subject_ref FROM approval_identities ORDER BY id")
                    )
                ).tuples().all() == history_before
                assert (
                    await connection.execute(
                        text(
                            """
                            SELECT id, subject_ref, policy_hash
                            FROM buyer_policies
                            WHERE id = 'pol_00000000000000000000000011'
                            """
                        )
                    )
                ).tuples().one() == policy_before
                assert (
                    await connection.execute(
                        text(
                            """
                            SELECT
                                id,
                                subject_ref,
                                policy_hash,
                                quote_hash,
                                review_hash,
                                challenge_hash,
                                authorization_hash
                            FROM purchase_authorizations
                            WHERE id = 'aut_00000000000000000000000011'
                            """
                        )
                    )
                ).tuples().one() == authorization_before

                await connection.run_sync(_alembic_downgrade_to_trusted_approval)

                def trusted_approval_tables(sync_connection: Any) -> set[str]:
                    return set(inspect(sync_connection).get_table_names())

                assert "accounts" not in await connection.run_sync(trusted_approval_tables)
                assert (
                    await connection.execute(
                        text("SELECT id, subject_ref FROM approval_identities ORDER BY id")
                    )
                ).tuples().all() == history_before
                assert (
                    await connection.execute(
                        text(
                            """
                            SELECT
                                id,
                                subject_ref,
                                policy_hash,
                                quote_hash,
                                review_hash,
                                challenge_hash,
                                authorization_hash
                            FROM purchase_authorizations
                            WHERE id = 'aut_00000000000000000000000011'
                            """
                        )
                    )
                ).tuples().one() == authorization_before

                await connection.run_sync(_alembic_upgrade)
                await connection.run_sync(_alembic_check)
                rebackfilled_ids = await connection.scalars(
                    text("SELECT id FROM accounts ORDER BY id")
                )
                assert list(rebackfilled_ids.all()) == [
                    "acct_00000000000000000000000011",
                    "acct_00000000000000000000000012",
                    "acct_00000000000000000000000013",
                ]
        finally:
            await isolated.database.dispose()
            await _drop_isolated_schema(isolated.database_url, isolated.schema)

    asyncio.run(exercise_cycle())


def test_approval_records_enforce_atomic_usage_and_database_immutability(
    domain_client: TestClient,
    isolated_database: IsolatedDatabase,
) -> None:
    raw_credential_id = b"postgresql-approval-credential"
    current, credential = asyncio.run(
        create_authenticated_account(
            isolated_database,
            display_name="PostgreSQL Approval User",
            raw_credential_id=raw_credential_id,
        )
    )
    subject_ref = current.account.id
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
    with authenticated_domain_client(domain_client, current) as authenticated_client:
        policy_response = authenticated_client.post(
            "/api/v1/policies",
            json={
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
        assert policy["subject_ref"] == subject_ref
        evaluation_response = authenticated_client.post(
            "/api/v1/policy-evaluations",
            json={"policy_id": policy["id"], "quote_id": quote["id"]},
        )
        assert evaluation_response.status_code == 201, evaluation_response.text
        evaluation = evaluation_response.json()
    assert evaluation["decision"] == "allow"
    authorized_at = datetime.now(UTC)
    expires_at = authorized_at + timedelta(minutes=2)
    review_hash = sha256_bytes(b"postgresql-approval-review")
    challenge_hash = sha256_bytes(b"postgresql-approval-challenge")

    def build_authorization(authorization_id: str) -> PurchaseAuthorization:
        authorization_hash = calculate_authorization_hash(
            authorization_id=authorization_id,
            authorization_version=AUTHORIZATION_VERSION,
            approval_identity_id=current.approval_identity.id,
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
            approval_identity_id=current.approval_identity.id,
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
                identity_id=current.approval_identity.id,
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


def test_payment_repository_commits_audited_aggregate_and_postgresql_guards(
    domain_client: TestClient,
    isolated_database: IsolatedDatabase,
) -> None:
    raw_credential_id = f"payment-persistence-{uuid.uuid4().hex}".encode()
    current, credential = asyncio.run(
        create_authenticated_account(
            isolated_database,
            display_name="PostgreSQL Payment User",
            raw_credential_id=raw_credential_id,
        )
    )
    merchant = create_merchant(
        domain_client,
        f"payment-persistence-{uuid.uuid4().hex[:12]}",
    )
    service = create_service(
        domain_client,
        merchant["id"],
        f"payment-service-{uuid.uuid4().hex[:12]}",
        base_price=500,
    )
    quote_response = domain_client.post(
        "/api/v1/quotes",
        json={"service_id": service["id"], "input": {"norad_id": 25544}},
    )
    assert quote_response.status_code == 201, quote_response.text
    quote = quote_response.json()
    with authenticated_domain_client(domain_client, current) as authenticated_client:
        policy_response = authenticated_client.post(
            "/api/v1/policies",
            json={
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
        evaluation_response = authenticated_client.post(
            "/api/v1/policy-evaluations",
            json={"policy_id": policy["id"], "quote_id": quote["id"]},
        )
        assert evaluation_response.status_code == 201, evaluation_response.text
        evaluation = evaluation_response.json()
    assert evaluation["decision"] == "allow"

    authorized_at = datetime.now(UTC)
    expires_at = authorized_at + timedelta(minutes=2)
    authorization_id = new_authorization_id()
    review_hash = sha256_bytes(b"payment-persistence-review")
    challenge_hash = sha256_bytes(uuid.uuid4().bytes)
    authorization_hash = calculate_authorization_hash(
        authorization_id=authorization_id,
        authorization_version=AUTHORIZATION_VERSION,
        approval_identity_id=current.approval_identity.id,
        passkey_credential_id=credential.id,
        subject_ref=current.account.id,
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
    authorization = PurchaseAuthorization(
        id=authorization_id,
        approval_identity_id=current.approval_identity.id,
        passkey_credential_id=credential.id,
        evaluation_id=evaluation["id"],
        policy_id=policy["id"],
        policy_hash=policy["policy_hash"],
        quote_id=quote["id"],
        quote_hash=quote["quote_hash"],
        merchant_id=merchant["id"],
        service_id=service["id"],
        subject_ref=current.account.id,
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

    async def persist_authorization() -> None:
        async with isolated_database.database.session() as session:
            locked_credential = await PasskeyCredentialRepository(
                session
            ).get_by_credential_id_for_update(
                raw_credential_id,
                identity_id=current.approval_identity.id,
            )
            assert locked_credential is not None
            await PurchaseAuthorizationRepository(session).create_with_locked_credential_update(
                authorization,
                credential=locked_credential,
                new_sign_count=1,
                last_used_at=authorized_at,
            )

    asyncio.run(persist_authorization())

    def transaction_bundle(
        *,
        transaction_id: str,
        occurred_at: datetime,
        idempotency_suffix: str,
    ) -> tuple[PaymentTransaction, PaymentTransactionEvent]:
        binding_fields: dict[str, Any] = {
            "transaction_id": transaction_id,
            "payment_binding_version": PAYMENT_BINDING_VERSION,
            "account_id": current.account.id,
            "authorization_id": authorization.id,
            "authorization_hash": authorization.authorization_hash,
            "evaluation_id": authorization.evaluation_id,
            "policy_id": authorization.policy_id,
            "policy_hash": authorization.policy_hash,
            "quote_id": authorization.quote_id,
            "quote_hash": authorization.quote_hash,
            "merchant_id": authorization.merchant_id,
            "service_id": authorization.service_id,
            "amount": authorization.amount,
            "currency": authorization.currency,
            "purchase_type": authorization.purchase_type,
            "provider": PaymentProvider.RAZORPAY,
            "provider_receipt": transaction_id,
        }
        transaction = PaymentTransaction(
            id=transaction_id,
            account_id=current.account.id,
            authorization_id=authorization.id,
            authorization_hash=authorization.authorization_hash,
            evaluation_id=authorization.evaluation_id,
            policy_id=authorization.policy_id,
            policy_hash=authorization.policy_hash,
            quote_id=authorization.quote_id,
            quote_hash=authorization.quote_hash,
            merchant_id=authorization.merchant_id,
            service_id=authorization.service_id,
            amount=authorization.amount,
            currency=authorization.currency,
            purchase_type=authorization.purchase_type,
            provider=PaymentProvider.RAZORPAY,
            provider_receipt=transaction_id,
            transaction_state=PaymentTransactionState.ORDER_CREATION_PENDING,
            order_creation_attempts=1,
            order_creation_started_at=occurred_at,
            payment_binding_version=PAYMENT_BINDING_VERSION,
            payment_binding_hash=calculate_payment_binding_hash(**binding_fields),
            revision=1,
        )
        event = PaymentTransactionEvent(
            id=new_payment_transaction_event_id(),
            transaction_id=transaction_id,
            transaction_revision=1,
            event_type=PaymentTransactionEventType.PAYMENT_TRANSACTION_CREATED,
            actor_type=PaymentEventActorType.ACCOUNT,
            actor_id=current.account.id,
            prior_state=None,
            resulting_state=PaymentTransactionState.ORDER_CREATION_PENDING,
            reason_code="PAYMENT_TRANSACTION_CREATED",
            event_metadata={"authorization_id": authorization.id},
            idempotency_key=f"{transaction_id}:{idempotency_suffix}",
            occurred_at=occurred_at,
        )
        return transaction, event

    started_at = datetime.now(UTC)
    transaction, initial_event = transaction_bundle(
        transaction_id=new_payment_transaction_id(),
        occurred_at=started_at,
        idempotency_suffix="claim",
    )

    duplicate, duplicate_event = transaction_bundle(
        transaction_id=new_payment_transaction_id(),
        occurred_at=started_at,
        idempotency_suffix="duplicate",
    )

    async def race_authorization_claims() -> tuple[bool, bool]:
        barrier = asyncio.Barrier(2)

        async def claim(
            candidate: PaymentTransaction,
            event: PaymentTransactionEvent,
        ) -> bool:
            async with isolated_database.database.session() as session:
                await barrier.wait()
                try:
                    await PaymentTransactionRepository(session).create_with_event(
                        candidate,
                        event=event,
                    )
                except IntegrityError:
                    return False
                return True

        first, second = await asyncio.gather(
            claim(transaction, initial_event),
            claim(duplicate, duplicate_event),
        )
        return first, second

    claim_results = asyncio.run(race_authorization_claims())
    assert sorted(claim_results) == [False, True]
    transaction = transaction if claim_results[0] else duplicate

    async def claimed_transaction_ids() -> list[str]:
        async with isolated_database.database.session() as session:
            result = await session.scalars(
                select(PaymentTransaction.id).where(
                    PaymentTransaction.authorization_id == authorization.id
                )
            )
            return list(result)

    assert asyncio.run(claimed_transaction_ids()) == [transaction.id]

    order_id = f"order_{uuid.uuid4().hex}"

    async def bind_order() -> None:
        async with isolated_database.database.session() as session:
            repository = PaymentTransactionRepository(session)
            locked = await repository.get_for_update(transaction.id)
            assert locked is not None
            prior_state = PaymentTransactionState(locked.transaction_state)
            observed_at = started_at + timedelta(seconds=1)
            locked.provider_order_id = order_id
            locked.provider_order_status = RazorpayOrderStatus.CREATED
            locked.transaction_state = PaymentTransactionState.ORDER_CREATED
            locked.order_created_at = observed_at
            event = PaymentTransactionEvent(
                id=new_payment_transaction_event_id(),
                transaction_id=locked.id,
                transaction_revision=locked.revision + 1,
                event_type=PaymentTransactionEventType.RAZORPAY_ORDER_CREATED,
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=order_id,
                prior_state=prior_state,
                resulting_state=PaymentTransactionState.ORDER_CREATED,
                reason_code="PAYMENT_ORDER_CREATED",
                event_metadata={"provider_order_id": order_id},
                idempotency_key=f"{locked.id}:order:{order_id}",
                occurred_at=observed_at,
            )
            await repository.update_with_event(locked, event=event)

    asyncio.run(bind_order())

    first_attempt_id = new_payment_attempt_id()
    second_attempt_id = new_payment_attempt_id()
    webhook_id = new_razorpay_webhook_event_id()

    async def persist_multiple_attempts_and_webhook() -> None:
        async with isolated_database.database.session() as session:
            repository = PaymentTransactionRepository(session)
            locked = await repository.get_for_update(transaction.id)
            assert locked is not None
            observed_at = started_at + timedelta(seconds=2)
            attempts = tuple(
                PaymentAttempt(
                    id=attempt_id,
                    transaction_id=locked.id,
                    provider=PaymentProvider.RAZORPAY,
                    provider_order_id=order_id,
                    provider_payment_id=f"pay_{uuid.uuid4().hex}",
                    amount=locked.amount,
                    currency=locked.currency,
                    provider_status=PaymentAttemptStatus.FAILED,
                    method="upi",
                    captured=False,
                    provider_created_at=observed_at,
                    first_seen_at=observed_at,
                    last_seen_at=observed_at,
                )
                for attempt_id in (first_attempt_id, second_attempt_id)
            )
            webhook = RazorpayWebhookEvent(
                id=webhook_id,
                provider_event_id=f"event_{uuid.uuid4().hex}",
                provider_event_type="payment.failed",
                raw_body_hash=sha256_bytes(b"payment-persistence-webhook"),
                provider_created_at=observed_at,
                received_at=observed_at,
                processed_at=observed_at,
                processing_status=WebhookProcessingStatus.PROCESSED,
                processing_reason_code="PAYMENT_ATTEMPT_FAILED",
                provider_order_id=order_id,
                provider_payment_id=attempts[0].provider_payment_id,
                transaction_id=locked.id,
            )
            prior_state = PaymentTransactionState(locked.transaction_state)
            locked.provider_order_status = RazorpayOrderStatus.ATTEMPTED
            locked.transaction_state = PaymentTransactionState.PAYMENT_PENDING
            locked.last_reconciled_at = observed_at
            event = PaymentTransactionEvent(
                id=new_payment_transaction_event_id(),
                transaction_id=locked.id,
                transaction_revision=locked.revision + 1,
                event_type=PaymentTransactionEventType.PAYMENT_ATTEMPT_FAILED,
                actor_type=PaymentEventActorType.PROVIDER_WEBHOOK,
                actor_id=webhook.provider_event_id,
                prior_state=prior_state,
                resulting_state=PaymentTransactionState.PAYMENT_PENDING,
                reason_code="PAYMENT_ATTEMPT_FAILED",
                event_metadata={"payment_count": len(attempts)},
                payment_attempt_id=attempts[0].id,
                source_webhook_event_id=webhook.id,
                idempotency_key=f"{locked.id}:webhook:{webhook.provider_event_id}",
                occurred_at=observed_at,
            )
            await repository.update_with_event(
                locked,
                event=event,
                attempts=attempts,
                webhook_event=webhook,
            )

    asyncio.run(persist_multiple_attempts_and_webhook())

    async def capture_first_attempt() -> None:
        async with isolated_database.database.session() as session:
            repository = PaymentTransactionRepository(session)
            locked = await repository.get_for_update(transaction.id)
            stored_attempt = await session.get(PaymentAttempt, first_attempt_id)
            assert stored_attempt is not None
            attempt = await PaymentAttemptRepository(session).get_by_provider_payment_id_for_update(
                stored_attempt.provider_payment_id
            )
            assert locked is not None
            assert attempt is not None
            observed_at = started_at + timedelta(seconds=3)
            prior_state = PaymentTransactionState(locked.transaction_state)
            attempt.provider_status = PaymentAttemptStatus.CAPTURED
            attempt.captured = True
            attempt.last_seen_at = observed_at
            locked.provider_order_status = RazorpayOrderStatus.PAID
            locked.transaction_state = PaymentTransactionState.PAID
            locked.paid_at = observed_at
            locked.last_reconciled_at = observed_at
            event = PaymentTransactionEvent(
                id=new_payment_transaction_event_id(),
                transaction_id=locked.id,
                transaction_revision=locked.revision + 1,
                event_type=PaymentTransactionEventType.PAYMENT_CAPTURED,
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=attempt.provider_payment_id,
                prior_state=prior_state,
                resulting_state=PaymentTransactionState.PAID,
                reason_code="PAYMENT_CAPTURED",
                event_metadata={"provider_payment_id": attempt.provider_payment_id},
                payment_attempt_id=attempt.id,
                idempotency_key=f"{locked.id}:capture:{attempt.provider_payment_id}",
                occurred_at=observed_at,
            )
            await repository.update_with_event(locked, event=event, attempt=attempt)

    asyncio.run(capture_first_attempt())

    async def capture_second_attempt() -> None:
        async with isolated_database.database.session() as session:
            repository = PaymentTransactionRepository(session)
            locked = await repository.get_for_update(transaction.id)
            attempt = await session.get(PaymentAttempt, second_attempt_id)
            assert locked is not None
            assert attempt is not None
            observed_at = started_at + timedelta(seconds=4)
            attempt.provider_status = PaymentAttemptStatus.CAPTURED
            attempt.captured = True
            attempt.last_seen_at = observed_at
            event = PaymentTransactionEvent(
                id=new_payment_transaction_event_id(),
                transaction_id=locked.id,
                transaction_revision=locked.revision + 1,
                event_type=PaymentTransactionEventType.PAYMENT_CAPTURED,
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=attempt.provider_payment_id,
                prior_state=PaymentTransactionState.PAID,
                resulting_state=PaymentTransactionState.PAID,
                reason_code="PAYMENT_MULTIPLE_CAPTURES_DETECTED",
                event_metadata={},
                payment_attempt_id=attempt.id,
                idempotency_key=f"{locked.id}:capture:{attempt.provider_payment_id}",
                occurred_at=observed_at,
            )
            await repository.update_with_event(locked, event=event, attempt=attempt)

    with pytest.raises(IntegrityError):
        asyncio.run(capture_second_attempt())

    async def read_persisted_state() -> tuple[str, int, int, int]:
        async with isolated_database.database.session() as session:
            persisted = await PaymentTransactionRepository(session).get(transaction.id)
            assert persisted is not None
            captured_count = await session.scalar(
                text(
                    "SELECT count(*) FROM payment_attempts "
                    "WHERE transaction_id = :transaction_id AND captured"
                ),
                {"transaction_id": transaction.id},
            )
            event_count = await session.scalar(
                text(
                    "SELECT count(*) FROM payment_transaction_events "
                    "WHERE transaction_id = :transaction_id"
                ),
                {"transaction_id": transaction.id},
            )
            webhook_count = await session.scalar(
                text(
                    "SELECT count(*) FROM razorpay_webhook_events "
                    "WHERE transaction_id = :transaction_id"
                ),
                {"transaction_id": transaction.id},
            )
            assert captured_count is not None
            assert event_count is not None
            assert webhook_count is not None
            return (
                PaymentTransactionState(persisted.transaction_state).value,
                persisted.revision,
                captured_count,
                event_count + webhook_count,
            )

    assert asyncio.run(read_persisted_state()) == ("paid", 4, 1, 5)

    async def execute(statement: str, parameters: dict[str, object]) -> None:
        async with isolated_database.database.engine.begin() as connection:
            await connection.execute(text(statement), parameters)

    with pytest.raises(DBAPIError):
        asyncio.run(
            execute(
                """
                UPDATE payment_transactions
                SET last_reconciled_at = :observed_at,
                    updated_at = :observed_at,
                    revision = revision + 1
                WHERE id = :transaction_id
                """,
                {
                    "observed_at": started_at + timedelta(minutes=1),
                    "transaction_id": transaction.id,
                },
            )
        )
    with pytest.raises(DBAPIError):
        asyncio.run(
            execute(
                """
                UPDATE payment_attempts
                SET provider_status = 'authorized'
                WHERE id = :attempt_id
                """,
                {"attempt_id": first_attempt_id},
            )
        )
    with pytest.raises(DBAPIError):
        asyncio.run(
            execute(
                """
                UPDATE payment_transaction_events
                SET reason_code = 'CHANGED'
                WHERE transaction_id = :transaction_id
                """,
                {"transaction_id": transaction.id},
            )
        )

    async def prepare_entitlement() -> Entitlement:
        async with isolated_database.database.session() as session:
            payment_repository = PaymentTransactionRepository(session)
            locked = await payment_repository.get_for_update(transaction.id)
            attempt = await session.get(PaymentAttempt, first_attempt_id)
            quote_record = await session.get(Quote, transaction.quote_id)
            assert locked is not None
            assert attempt is not None
            assert quote_record is not None
            assert locked.last_reconciled_at is not None
            reverified_at = max(
                datetime.now(UTC),
                locked.last_reconciled_at + timedelta(microseconds=1),
            )
            proof_event = PaymentTransactionEvent(
                id=new_payment_transaction_event_id(),
                transaction_id=locked.id,
                transaction_revision=locked.revision + 1,
                event_type=PaymentTransactionEventType.PAYMENT_REVERIFIED,
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=attempt.provider_payment_id,
                prior_state=PaymentTransactionState.PAID,
                resulting_state=PaymentTransactionState.PAID,
                reason_code="PAYMENT_REVERIFIED_FOR_VALUE_RELEASE",
                event_metadata={"authoritative_snapshot": True},
                payment_attempt_id=attempt.id,
                idempotency_key=f"{locked.id}:concurrency-proof",
                occurred_at=reverified_at,
            )
            locked.last_reconciled_at = reverified_at
            locked = await payment_repository.update_with_event(locked, event=proof_event)

            entitlement_id = new_entitlement_id()
            issued_at = reverified_at + timedelta(microseconds=1)
            expires_at = issued_at + timedelta(minutes=10)
            hash_fields = {
                "entitlement_id": entitlement_id,
                "account_id": locked.account_id,
                "transaction_id": locked.id,
                "payment_binding_hash": locked.payment_binding_hash,
                "payment_reverification_event_id": proof_event.id,
                "payment_reverification_revision": locked.revision,
                "provider_order_id": locked.provider_order_id,
                "provider_payment_id": attempt.provider_payment_id,
                "authorization_id": locked.authorization_id,
                "authorization_hash": locked.authorization_hash,
                "evaluation_id": locked.evaluation_id,
                "policy_id": locked.policy_id,
                "policy_hash": locked.policy_hash,
                "quote_id": locked.quote_id,
                "quote_hash": locked.quote_hash,
                "merchant_id": locked.merchant_id,
                "service_id": locked.service_id,
                "input_value": quote_record.input,
                "input_hash": quote_record.input_hash,
                "amount": locked.amount,
                "currency": locked.currency,
                "purchase_type": locked.purchase_type,
                "maximum_executions": 1,
                "issued_at": issued_at,
                "expires_at": expires_at,
                "entitlement_version": "1",
            }
            entitlement = Entitlement(
                id=entitlement_id,
                account_id=locked.account_id,
                transaction_id=locked.id,
                payment_binding_hash=locked.payment_binding_hash,
                payment_reverification_event_id=proof_event.id,
                payment_reverification_revision=locked.revision,
                provider_order_id=locked.provider_order_id,
                provider_payment_id=attempt.provider_payment_id,
                authorization_id=locked.authorization_id,
                authorization_hash=locked.authorization_hash,
                evaluation_id=locked.evaluation_id,
                policy_id=locked.policy_id,
                policy_hash=locked.policy_hash,
                quote_id=locked.quote_id,
                quote_hash=locked.quote_hash,
                merchant_id=locked.merchant_id,
                service_id=locked.service_id,
                input=quote_record.input,
                input_hash=quote_record.input_hash,
                amount=locked.amount,
                currency=locked.currency,
                purchase_type=locked.purchase_type,
                maximum_executions=1,
                issued_at=issued_at,
                expires_at=expires_at,
                entitlement_version="1",
                entitlement_hash=calculate_entitlement_hash(**hash_fields),
                created_at=issued_at,
            )
            issuance_event = FulfillmentEvent(
                id=new_fulfillment_event_id(),
                transaction_id=locked.id,
                entitlement_id=entitlement.id,
                execution_id=None,
                execution_revision=None,
                event_type=FulfillmentEventType.ENTITLEMENT_ISSUED,
                actor_type=FulfillmentEventActorType.ENTITLEMENT_WORKER,
                actor_id=None,
                reason_code="ENTITLEMENT_ISSUED",
                event_metadata={},
                idempotency_key=f"{locked.id}:concurrency-entitlement",
                occurred_at=issued_at,
            )
            entitlement = await EntitlementRepository(session).create_with_event(
                entitlement,
                event=issuance_event,
            )
            await ServiceFulfillmentConfigRepository(session).create(
                ServiceFulfillmentConfig(
                    service_id=locked.service_id,
                    provider_type=FulfillmentProviderType.HTTP,
                    endpoint_url="http://127.0.0.1:8100/internal/v1/fulfillments",
                    request_timeout_seconds=10,
                    maximum_attempts=3,
                    enabled=True,
                    revision=1,
                )
            )
            return entitlement

    entitlement = asyncio.run(prepare_entitlement())

    class BlockingFulfillmentProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(
            self,
            request: object,
            *,
            endpoint_path: str,
        ) -> MerchantFulfillmentResult:
            del request
            assert endpoint_path == "/internal/v1/fulfillments"
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return MerchantFulfillmentResult(
                result_content_type="application/json",
                result={"data_source": "CelesTrak", "norad_id": 25544},
            )

    class ConcurrencyPaymentEligibility:
        def __init__(self, session: object) -> None:
            self._payments = PaymentApplicationService(
                session,  # type: ignore[arg-type]
                None,
                domain_client.app.state.settings,
            )

        async def verify_transaction_for_value_release(self, transaction_id: str) -> object:
            # Provider-proof behavior is covered separately; this PostgreSQL test
            # isolates the row-lock/unique-key execution race.
            return await self._payments.require_local_value_release_eligibility(transaction_id)

        async def require_local_value_release_eligibility(
            self,
            transaction_id: str,
            *,
            for_update: bool = False,
        ) -> object:
            return await self._payments.require_local_value_release_eligibility(
                transaction_id,
                for_update=for_update,
            )

    async def run_concurrent_execution() -> tuple[int, int, int, bool, bool, int]:
        provider = BlockingFulfillmentProvider()
        capability_service = CapabilityTokenService(
            "postgresql-concurrency-capability-secret-32-bytes",
            ttl=timedelta(minutes=5),
        )
        token = capability_service.issue(entitlement).token

        async def invoke() -> object:
            async with isolated_database.database.session() as session:
                return await FulfillmentApplicationService(
                    session,
                    capability_service,
                    provider,
                    payment_eligibility=ConcurrencyPaymentEligibility(session),
                    provider_base_url="http://127.0.0.1:8100",
                    execution_lease=timedelta(seconds=30),
                    maximum_result_bytes=262_144,
                    default_maximum_attempts=3,
                ).execute(
                    merchant_slug=merchant["slug"],
                    service_slug=service["slug"],
                    input_value={"norad_id": 25544},
                    token=token,
                )

        first_task = asyncio.create_task(invoke())
        await provider.started.wait()
        concurrent = await invoke()
        provider.release.set()
        first = await first_task
        replay = await invoke()
        async with isolated_database.database.session() as session:
            execution_count = await session.scalar(
                select(func.count(FulfillmentExecution.id)).where(
                    FulfillmentExecution.entitlement_id == entitlement.id
                )
            )
        assert execution_count is not None
        return (
            first.status_code,
            concurrent.status_code,
            replay.status_code,
            first.result.replayed_result,
            replay.result.replayed_result,
            execution_count,
        )

    assert asyncio.run(run_concurrent_execution()) == (200, 202, 200, False, True, 1)


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
    isolated_database: IsolatedDatabase,
) -> None:
    current, _ = asyncio.run(
        create_authenticated_account(
            isolated_database,
            display_name="PostgreSQL Policy User",
            raw_credential_id=b"postgresql-policy-api-credential",
        )
    )
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
        "allowed_currencies": ["INR"],
        "allowed_merchant_ids": [merchant["id"]],
        "allowed_service_ids": [service["id"]],
        "allowed_service_types": ["report"],
        "allowed_purchase_types": ["one_time"],
        "expires_in_seconds": 900,
    }
    with authenticated_domain_client(domain_client, current) as authenticated_client:
        allow_response = authenticated_client.post(
            "/api/v1/policies",
            json={**common_policy, "maximum_amount": 1_000},
        )
        deny_response = authenticated_client.post(
            "/api/v1/policies",
            json={**common_policy, "maximum_amount": 100},
        )
        assert allow_response.status_code == 201, allow_response.text
        assert deny_response.status_code == 201, deny_response.text
        allow_policy = allow_response.json()
        deny_policy = deny_response.json()
        assert ID_PATTERN.fullmatch(allow_policy["id"])
        assert allow_policy["subject_ref"] == current.account.id
        assert deny_policy["subject_ref"] == current.account.id
        assert allow_policy["constraints"]["maximum_amount"] == 1_000
        assert allow_policy["state"] == "active"
        assert (
            authenticated_client.get(f"/api/v1/policies/{allow_policy['id']}").json()
            == allow_policy
        )

        allow_evaluation_response = authenticated_client.post(
            "/api/v1/policy-evaluations",
            json={"policy_id": allow_policy["id"], "quote_id": quote["id"]},
        )
        deny_evaluation_response = authenticated_client.post(
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
            authenticated_client.get(f"/api/v1/policy-evaluations/{allow_evaluation['id']}").json()
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
