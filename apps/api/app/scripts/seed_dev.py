"""Idempotently seed synthetic local-development catalog records."""

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.db.session import Database, create_database
from app.domain.enums import (
    FulfillmentProviderType,
    MerchantStatus,
    PurchaseType,
    ServiceStatus,
    ServiceType,
)
from app.domain.hashing import MAX_CANONICAL_INTEGER
from app.models import Merchant, Service, ServiceFulfillmentConfig
from app.repositories.merchants import MerchantRepository
from app.repositories.service_fulfillment_configs import ServiceFulfillmentConfigRepository
from app.repositories.services import ServiceRepository
from app.schemas.merchants import MerchantCreate
from app.schemas.services import ServiceCreate

ORBITINTEL = MerchantCreate(
    slug="orbitintel",
    name="OrbitIntel",
    description="Deterministic satellite intelligence from current CelesTrak GP data.",
    status=MerchantStatus.ACTIVE,
)

_RISK_DISCLAIMER = "This is not a conjunction warning or operational collision-risk product."
_ORBITAL_REGIMES = [
    "suborbital_or_decayed",
    "low_earth_orbit",
    "geosynchronous_like",
    "highly_elliptical_orbit",
    "medium_earth_orbit",
    "high_earth_orbit",
]


def _timestamp_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "format": "date-time",
        "minLength": 20,
        "maxLength": 64,
    }


def _norad_id_schema() -> dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": 999_999_999}


def _object_name_schema() -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": 256}


def _international_designator_schema() -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": 64}


def _source_orbit_properties() -> dict[str, Any]:
    return {
        "norad_id": _norad_id_schema(),
        "object_name": _object_name_schema(),
        "international_designator": _international_designator_schema(),
        "epoch": _timestamp_schema(),
        "mean_motion_rev_per_day": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 100,
        },
        "eccentricity": {
            "type": "number",
            "minimum": 0,
            "maximum": 0.999999999999,
        },
        "inclination_degrees": {"type": "number", "minimum": 0, "maximum": 180},
        "raan_degrees": {"type": "number", "minimum": 0, "maximum": 360},
        "argument_of_pericenter_degrees": {
            "type": "number",
            "minimum": 0,
            "maximum": 360,
        },
        "mean_anomaly_degrees": {"type": "number", "minimum": 0, "maximum": 360},
        "classification_type": {"type": "string", "minLength": 1, "maxLength": 8},
        "element_set_number": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_CANONICAL_INTEGER,
        },
        "revolution_number_at_epoch": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_CANONICAL_INTEGER,
        },
        "bstar": {"type": "number", "minimum": -1_000_000, "maximum": 1_000_000},
        "mean_motion_dot": {
            "type": "number",
            "minimum": -1_000_000,
            "maximum": 1_000_000,
        },
        "mean_motion_ddot": {
            "type": "number",
            "minimum": -1_000_000,
            "maximum": 1_000_000,
        },
    }


def _derived_orbit_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "orbital_period_minutes": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 1_000_000,
            },
            "semi_major_axis_km": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 1_000_000_000,
            },
            "approximate_perigee_altitude_km": {
                "type": "number",
                "minimum": -10_000,
                "maximum": 1_000_000_000,
            },
            "approximate_apogee_altitude_km": {
                "type": "number",
                "minimum": -10_000,
                "maximum": 1_000_000_000,
            },
            "orbital_regime": {"type": "string", "enum": _ORBITAL_REGIMES},
        },
        "required": [
            "orbital_period_minutes",
            "semi_major_axis_km",
            "approximate_perigee_altitude_km",
            "approximate_apogee_altitude_km",
            "orbital_regime",
        ],
        "additionalProperties": False,
    }


def _freshness_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "element_set_age_hours": {
                "type": "number",
                "minimum": -876_000,
                "maximum": 876_000,
            },
            "data_freshness_indicator": {
                "type": "string",
                "enum": ["future_epoch", "current", "aging", "stale"],
            },
        },
        "required": ["element_set_age_hours", "data_freshness_indicator"],
        "additionalProperties": False,
    }


def _heuristic_indicators_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "eccentricity_indicator": {
                "type": "string",
                "enum": ["near_circular", "low", "moderate", "high"],
            },
            "very_low_perigee_indicator": {"type": "boolean"},
        },
        "required": ["eccentricity_indicator", "very_low_perigee_indicator"],
        "additionalProperties": False,
    }


def _calculation_metadata_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "model": {"const": "two_body_keplerian_approximation"},
            "earth_gravitational_parameter_km3_s2": {"const": 398_600.4418},
            "earth_reference_radius_km": {"const": 6_378.137},
            "formulas": {
                "const": {
                    "orbital_period_minutes": "1440 / mean_motion_rev_per_day",
                    "semi_major_axis_km": (
                        "(mu / (mean_motion_rev_per_day * 2*pi / 86400)^2)^(1/3)"
                    ),
                    "approximate_perigee_altitude_km": (
                        "semi_major_axis_km * (1 - eccentricity) - earth_radius_km"
                    ),
                    "approximate_apogee_altitude_km": (
                        "semi_major_axis_km * (1 + eccentricity) - earth_radius_km"
                    ),
                }
            },
            "approximation_notes": {
                "const": [
                    (
                        "Derived altitudes use a two-body Keplerian approximation from "
                        "current GP elements."
                    ),
                    "Earth is represented by the WGS 84 semi-major-axis reference radius.",
                    (
                        "Atmospheric drag, perturbation propagation, and conjunction geometry "
                        "are not evaluated."
                    ),
                ]
            },
        },
        "required": [
            "model",
            "earth_gravitational_parameter_km3_s2",
            "earth_reference_radius_km",
            "formulas",
            "approximation_notes",
        ],
        "additionalProperties": False,
    }


def _satellite_status_output_schema() -> dict[str, Any]:
    properties = {
        "result_type": {"const": "satellite_status"},
        "norad_id": _norad_id_schema(),
        "object_name": _object_name_schema(),
        "international_designator": _international_designator_schema(),
        "epoch": _timestamp_schema(),
        "mean_motion": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
        "eccentricity": {
            "type": "number",
            "minimum": 0,
            "maximum": 0.999999999999,
        },
        "inclination": {"type": "number", "minimum": 0, "maximum": 180},
        "raan": {"type": "number", "minimum": 0, "maximum": 360},
        "argument_of_pericenter": {"type": "number", "minimum": 0, "maximum": 360},
        "mean_anomaly": {"type": "number", "minimum": 0, "maximum": 360},
        "data_source": {"const": "CelesTrak"},
        "fetched_at": _timestamp_schema(),
    }
    return {
        "type": "object",
        "properties": properties,
        "required": [key for key in properties if key != "international_designator"],
        "additionalProperties": False,
    }


def _orbital_risk_output_schema() -> dict[str, Any]:
    properties = {
        "result_type": {"const": "orbital_risk_report"},
        "norad_id": _norad_id_schema(),
        "object_name": _object_name_schema(),
        "international_designator": _international_designator_schema(),
        "source_epoch": _timestamp_schema(),
        "element_set_age_hours": {
            "type": "number",
            "minimum": -876_000,
            "maximum": 876_000,
        },
        **_derived_orbit_schema()["properties"],
        "eccentricity_indicator": {
            "type": "string",
            "enum": ["near_circular", "low", "moderate", "high"],
        },
        "very_low_perigee_indicator": {"type": "boolean"},
        "data_freshness_indicator": {
            "type": "string",
            "enum": ["future_epoch", "current", "aging", "stale"],
        },
        "assessment_scope": {"const": "heuristic_orbital_condition"},
        "operational_collision_assessment": {"const": False},
        "disclaimer": {"const": _RISK_DISCLAIMER},
        "data_source": {"const": "CelesTrak"},
        "fetched_at": _timestamp_schema(),
        "result_timestamp": _timestamp_schema(),
    }
    return {
        "type": "object",
        "properties": properties,
        "required": [key for key in properties if key != "international_designator"],
        "additionalProperties": False,
    }


def _detailed_analysis_output_schema() -> dict[str, Any]:
    source_properties = _source_orbit_properties()
    optional_source_fields = {
        "international_designator",
        "classification_type",
        "element_set_number",
        "revolution_number_at_epoch",
        "bstar",
        "mean_motion_dot",
        "mean_motion_ddot",
    }
    properties = {
        "result_type": {"const": "detailed_orbital_analysis"},
        "source": {
            "type": "object",
            "properties": source_properties,
            "required": [key for key in source_properties if key not in optional_source_fields],
            "additionalProperties": False,
        },
        "derived": _derived_orbit_schema(),
        "freshness": _freshness_schema(),
        "heuristic_indicators": _heuristic_indicators_schema(),
        "calculation_metadata": _calculation_metadata_schema(),
        "assessment_scope": {"const": "heuristic_orbital_condition"},
        "operational_collision_assessment": {"const": False},
        "disclaimer": {"const": _RISK_DISCLAIMER},
        "data_source": {"const": "CelesTrak"},
        "source_timestamp": _timestamp_schema(),
        "fetched_at": _timestamp_schema(),
        "result_timestamp": _timestamp_schema(),
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


ORBITINTEL_SERVICES = (
    ServiceCreate(
        slug="satellite-status-lookup",
        name="Satellite Status Lookup",
        description="Return normalized current CelesTrak GP data for a NORAD catalog object.",
        status=ServiceStatus.ACTIVE,
        service_type=ServiceType.API,
        purchase_type=PurchaseType.ONE_TIME,
        currency="INR",
        base_price=200,
        input_schema={
            "type": "object",
            "properties": {"norad_id": {"type": "integer", "minimum": 1, "maximum": 999999999}},
            "required": ["norad_id"],
            "additionalProperties": False,
        },
        output_schema=_satellite_status_output_schema(),
        output_content_type="application/json",
        maximum_fulfillment_seconds=10,
        refund_on_fulfillment_failure=True,
    ),
    ServiceCreate(
        slug="orbital-risk-report",
        name="Orbital Risk Report",
        description=(
            "Generate a deterministic heuristic orbital-condition report from current "
            "CelesTrak GP data; not an operational conjunction warning."
        ),
        status=ServiceStatus.ACTIVE,
        service_type=ServiceType.REPORT,
        purchase_type=PurchaseType.ONE_TIME,
        currency="INR",
        base_price=500,
        input_schema={
            "type": "object",
            "properties": {"norad_id": {"type": "integer", "minimum": 1, "maximum": 999999999}},
            "required": ["norad_id"],
            "additionalProperties": False,
        },
        output_schema=_orbital_risk_output_schema(),
        output_content_type="application/json",
        maximum_fulfillment_seconds=30,
        refund_on_fulfillment_failure=True,
    ),
    ServiceCreate(
        slug="detailed-orbital-analysis",
        name="Detailed Orbital Analysis",
        description="Generate richer deterministic orbital analysis from current CelesTrak GP data.",
        status=ServiceStatus.ACTIVE,
        service_type=ServiceType.REPORT,
        purchase_type=PurchaseType.ONE_TIME,
        currency="INR",
        base_price=900,
        input_schema={
            "type": "object",
            "properties": {"norad_id": {"type": "integer", "minimum": 1, "maximum": 999999999}},
            "required": ["norad_id"],
            "additionalProperties": False,
        },
        output_schema=_detailed_analysis_output_schema(),
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


async def seed_development_data(
    database: Database,
    *,
    orbitintel_base_url: str = "http://127.0.0.1:8100",
) -> SeedResult:
    """Converge the configured database on the canonical synthetic seed records."""
    async with database.session() as session:
        merchants = MerchantRepository(session)
        services = ServiceRepository(session)
        fulfillment_configs = ServiceFulfillmentConfigRepository(session)

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
                service = await services.create(Service(merchant_id=merchant.id, **service_values))
                services_created += 1
            elif _apply_changes(service, service_values):
                service = await services.update(service)
                services_updated += 1
            else:
                services_unchanged += 1
            endpoint_url = f"{orbitintel_base_url.rstrip('/')}/internal/v1/fulfillments"
            fulfillment_config = await fulfillment_configs.get_by_service_id(service.id)
            config_values = {
                "endpoint_url": endpoint_url,
                "request_timeout_seconds": 15,
                "maximum_attempts": 3,
                "enabled": True,
            }
            if fulfillment_config is None:
                await fulfillment_configs.create(
                    ServiceFulfillmentConfig(
                        service_id=service.id,
                        provider_type=FulfillmentProviderType.HTTP,
                        revision=1,
                        **config_values,
                    )
                )
            elif _apply_changes(fulfillment_config, config_values):
                await fulfillment_configs.update(fulfillment_config)

    return SeedResult(
        merchant_created=merchant_created,
        merchant_updated=merchant_updated,
        services_created=services_created,
        services_updated=services_updated,
        services_unchanged=services_unchanged,
    )


async def _run() -> None:
    settings = get_settings()
    database = create_database(settings)
    try:
        result = await seed_development_data(
            database,
            orbitintel_base_url=settings.orbitintel_base_url,
        )
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
