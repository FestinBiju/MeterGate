"""Idempotently seed synthetic local-development catalog records."""

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.db.session import Database, create_database
from app.domain.enums import MerchantStatus, PurchaseType, ServiceStatus, ServiceType
from app.models import Merchant, Service
from app.repositories.merchants import MerchantRepository
from app.repositories.services import ServiceRepository
from app.schemas.merchants import MerchantCreate
from app.schemas.services import ServiceCreate

ORBITINTEL = MerchantCreate(
    slug="orbitintel",
    name="OrbitIntel",
    description="Satellite intelligence and orbital analysis services.",
    status=MerchantStatus.ACTIVE,
)

ORBITINTEL_SERVICES = (
    ServiceCreate(
        slug="satellite-status-lookup",
        name="Satellite Status Lookup",
        description="Return synthetic status metadata for a NORAD catalog identifier.",
        status=ServiceStatus.ACTIVE,
        service_type=ServiceType.API,
        purchase_type=PurchaseType.ONE_TIME,
        currency="INR",
        base_price=200,
        input_schema={
            "type": "object",
            "properties": {"norad_id": {"type": "integer", "minimum": 1}},
            "required": ["norad_id"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "norad_id": {"type": "integer"},
                "status": {"type": "string"},
                "observed_at": {"type": "string", "format": "date-time"},
            },
            "required": ["norad_id", "status", "observed_at"],
            "additionalProperties": False,
        },
        output_content_type="application/json",
        maximum_fulfillment_seconds=10,
        refund_on_fulfillment_failure=True,
    ),
    ServiceCreate(
        slug="orbital-risk-report",
        name="Orbital Risk Report",
        description="Generate a synthetic current orbital-risk analysis for a satellite.",
        status=ServiceStatus.ACTIVE,
        service_type=ServiceType.REPORT,
        purchase_type=PurchaseType.ONE_TIME,
        currency="INR",
        base_price=500,
        input_schema={
            "type": "object",
            "properties": {"norad_id": {"type": "integer", "minimum": 1}},
            "required": ["norad_id"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "norad_id": {"type": "integer"},
                "risk_level": {"type": "string"},
                "summary": {"type": "string"},
                "generated_at": {"type": "string", "format": "date-time"},
            },
            "required": ["norad_id", "risk_level", "summary", "generated_at"],
            "additionalProperties": False,
        },
        output_content_type="application/json",
        maximum_fulfillment_seconds=30,
        refund_on_fulfillment_failure=True,
    ),
    ServiceCreate(
        slug="detailed-orbital-analysis",
        name="Detailed Orbital Analysis",
        description="Generate a synthetic detailed orbital analysis for a satellite.",
        status=ServiceStatus.ACTIVE,
        service_type=ServiceType.REPORT,
        purchase_type=PurchaseType.ONE_TIME,
        currency="INR",
        base_price=900,
        input_schema={
            "type": "object",
            "properties": {
                "norad_id": {"type": "integer", "minimum": 1},
                "analysis_window_hours": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 168,
                },
            },
            "required": ["norad_id"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "norad_id": {"type": "integer"},
                "summary": {"type": "string"},
                "risk_factors": {"type": "array", "items": {"type": "string"}},
                "generated_at": {"type": "string", "format": "date-time"},
            },
            "required": ["norad_id", "summary", "risk_factors", "generated_at"],
            "additionalProperties": False,
        },
        output_content_type="application/json",
        maximum_fulfillment_seconds=60,
        refund_on_fulfillment_failure=True,
    ),
)


@dataclass(frozen=True, slots=True)
class SeedResult:
    merchant_created: bool
    merchant_updated: bool
    services_created: int
    services_updated: int
    services_unchanged: int


def _apply_changes(record: Merchant | Service, values: dict[str, Any]) -> bool:
    changed = False
    for field, value in values.items():
        if getattr(record, field) != value:
            setattr(record, field, value)
            changed = True
    return changed


async def seed_development_data(database: Database) -> SeedResult:
    """Converge the configured database on the canonical synthetic seed records."""
    async with database.session() as session:
        merchants = MerchantRepository(session)
        services = ServiceRepository(session)

        merchant_values = ORBITINTEL.model_dump()
        merchant = await merchants.get_by_slug(ORBITINTEL.slug)
        merchant_created = merchant is None
        merchant_updated = False
        if merchant is None:
            merchant = await merchants.create(Merchant(**merchant_values))
        elif _apply_changes(merchant, merchant_values):
            merchant = await merchants.update(merchant)
            merchant_updated = True

        services_created = 0
        services_updated = 0
        services_unchanged = 0
        for service_seed in ORBITINTEL_SERVICES:
            service_values = service_seed.model_dump()
            service = await services.get_by_slug(merchant.id, service_seed.slug)
            if service is None:
                await services.create(Service(merchant_id=merchant.id, **service_values))
                services_created += 1
            elif _apply_changes(service, service_values):
                await services.update(service)
                services_updated += 1
            else:
                services_unchanged += 1

    return SeedResult(
        merchant_created=merchant_created,
        merchant_updated=merchant_updated,
        services_created=services_created,
        services_updated=services_updated,
        services_unchanged=services_unchanged,
    )


async def _run() -> None:
    database = create_database(get_settings())
    try:
        result = await seed_development_data(database)
    except SQLAlchemyError:
        raise SystemExit(
            "Development seed failed. Confirm PostgreSQL is ready and run "
            "`uv run alembic upgrade head` first."
        ) from None
    finally:
        await database.dispose()

    merchant_action = (
        "created"
        if result.merchant_created
        else "updated"
        if result.merchant_updated
        else "unchanged"
    )
    print(
        "Development seed ready: "
        f"OrbitIntel {merchant_action}; "
        f"services created={result.services_created}, "
        f"updated={result.services_updated}, unchanged={result.services_unchanged}."
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
