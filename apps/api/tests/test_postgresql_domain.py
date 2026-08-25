import asyncio
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.application import create_app
from app.core.config import Settings, get_settings
from app.db.session import Database, make_async_database_url
from app.scripts.seed_dev import seed_development_data
from app.services.readiness import ReadinessService

API_ROOT = Path(__file__).resolve().parents[1]
ID_PATTERN = re.compile(r"^(mrc_|svc_)[0-7][0-9A-HJKMNP-TV-Z]{25}$")
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
        "input_schema": {
            "type": "object",
            "properties": {"norad_id": {"type": "integer"}},
            "required": ["norad_id"],
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
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/merchants/{merchant_id}/services",
        json=service_payload(slug, status=status, base_price=base_price),
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
    assert {"alembic_version", "merchants", "services"} <= tables
    assert {
        "ck_services_service_base_price_nonnegative",
        "ck_services_service_input_schema_object",
        "ck_services_service_output_schema_object",
    } <= checks
    assert "uq_services_merchant_id_slug" in named_constraints
    assert "fk_services_merchant_id_merchants" in named_constraints


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
