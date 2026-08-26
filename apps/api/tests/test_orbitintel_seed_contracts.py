"""Exact output contracts for the seeded OrbitIntel services."""

from copy import deepcopy
from typing import Any

import pytest

from app.domain.json_schema import validate_json_instance
from app.scripts.seed_dev import ORBITINTEL_SERVICES

TIMESTAMP = "2026-08-26T12:00:00Z"
DISCLAIMER = "This is not a conjunction warning or operational collision-risk product."

STATUS_RESULT = {
    "result_type": "satellite_status",
    "norad_id": 25544,
    "object_name": "ISS (ZARYA)",
    "international_designator": "1998-067A",
    "epoch": TIMESTAMP,
    "mean_motion": 15.5,
    "eccentricity": 0.0005,
    "inclination": 51.64,
    "raan": 122.5,
    "argument_of_pericenter": 73.2,
    "mean_anomaly": 10.4,
    "data_source": "CelesTrak",
    "fetched_at": TIMESTAMP,
}

RISK_RESULT = {
    "result_type": "orbital_risk_report",
    "norad_id": 25544,
    "object_name": "ISS (ZARYA)",
    "international_designator": "1998-067A",
    "source_epoch": TIMESTAMP,
    "element_set_age_hours": 6.0,
    "orbital_period_minutes": 92.9,
    "semi_major_axis_km": 6796.0,
    "approximate_perigee_altitude_km": 412.0,
    "approximate_apogee_altitude_km": 423.0,
    "orbital_regime": "low_earth_orbit",
    "eccentricity_indicator": "near_circular",
    "very_low_perigee_indicator": False,
    "data_freshness_indicator": "current",
    "assessment_scope": "heuristic_orbital_condition",
    "operational_collision_assessment": False,
    "disclaimer": DISCLAIMER,
    "data_source": "CelesTrak",
    "fetched_at": TIMESTAMP,
    "result_timestamp": TIMESTAMP,
}

DETAILED_RESULT = {
    "result_type": "detailed_orbital_analysis",
    "source": {
        "norad_id": 25544,
        "object_name": "ISS (ZARYA)",
        "international_designator": "1998-067A",
        "epoch": TIMESTAMP,
        "mean_motion_rev_per_day": 15.5,
        "eccentricity": 0.0005,
        "inclination_degrees": 51.64,
        "raan_degrees": 122.5,
        "argument_of_pericenter_degrees": 73.2,
        "mean_anomaly_degrees": 10.4,
        "classification_type": "U",
        "element_set_number": 999,
        "revolution_number_at_epoch": 52345,
        "bstar": 0.000123,
        "mean_motion_dot": 0.0001,
        "mean_motion_ddot": 0.0,
    },
    "derived": {
        "orbital_period_minutes": 92.9,
        "semi_major_axis_km": 6796.0,
        "approximate_perigee_altitude_km": 412.0,
        "approximate_apogee_altitude_km": 423.0,
        "orbital_regime": "low_earth_orbit",
    },
    "freshness": {
        "element_set_age_hours": 6.0,
        "data_freshness_indicator": "current",
    },
    "heuristic_indicators": {
        "eccentricity_indicator": "near_circular",
        "very_low_perigee_indicator": False,
    },
    "calculation_metadata": {
        "model": "two_body_keplerian_approximation",
        "earth_gravitational_parameter_km3_s2": 398600.4418,
        "earth_reference_radius_km": 6378.137,
        "formulas": {
            "orbital_period_minutes": "1440 / mean_motion_rev_per_day",
            "semi_major_axis_km": ("(mu / (mean_motion_rev_per_day * 2*pi / 86400)^2)^(1/3)"),
            "approximate_perigee_altitude_km": (
                "semi_major_axis_km * (1 - eccentricity) - earth_radius_km"
            ),
            "approximate_apogee_altitude_km": (
                "semi_major_axis_km * (1 + eccentricity) - earth_radius_km"
            ),
        },
        "approximation_notes": [
            "Derived altitudes use a two-body Keplerian approximation from current GP elements.",
            "Earth is represented by the WGS 84 semi-major-axis reference radius.",
            (
                "Atmospheric drag, perturbation propagation, and conjunction geometry are not "
                "evaluated."
            ),
        ],
    },
    "assessment_scope": "heuristic_orbital_condition",
    "operational_collision_assessment": False,
    "disclaimer": DISCLAIMER,
    "data_source": "CelesTrak",
    "source_timestamp": TIMESTAMP,
    "fetched_at": TIMESTAMP,
    "result_timestamp": TIMESTAMP,
}

RESULTS = {
    "satellite-status-lookup": STATUS_RESULT,
    "orbital-risk-report": RISK_RESULT,
    "detailed-orbital-analysis": DETAILED_RESULT,
}


def _schema(service_slug: str) -> dict[str, Any]:
    return next(
        service.output_schema for service in ORBITINTEL_SERVICES if service.slug == service_slug
    )


@pytest.mark.parametrize("service_slug", list(RESULTS))
def test_seeded_output_schema_accepts_only_the_complete_result_shape(
    service_slug: str,
) -> None:
    schema = _schema(service_slug)
    result = deepcopy(RESULTS[service_slug])

    assert validate_json_instance(result, schema) == ()
    assert schema["additionalProperties"] is False

    result["unexpected"] = True
    violations = validate_json_instance(result, schema)
    assert {violation.keyword for violation in violations} == {"additionalProperties"}


@pytest.mark.parametrize(
    ("service_slug", "mutate"),
    [
        (
            "satellite-status-lookup",
            lambda result: result.__setitem__("norad_id", 0),
        ),
        (
            "orbital-risk-report",
            lambda result: result.__setitem__("orbital_regime", "unknown"),
        ),
        (
            "detailed-orbital-analysis",
            lambda result: result["calculation_metadata"]["approximation_notes"].append(
                "uncontracted note"
            ),
        ),
    ],
)
def test_seeded_output_schema_rejects_out_of_contract_values(
    service_slug: str,
    mutate: Any,
) -> None:
    result = deepcopy(RESULTS[service_slug])
    mutate(result)

    assert validate_json_instance(result, _schema(service_slug))


def test_detailed_nested_objects_are_closed() -> None:
    result = deepcopy(DETAILED_RESULT)
    result["source"]["unexpected"] = True

    violations = validate_json_instance(
        result,
        _schema("detailed-orbital-analysis"),
    )

    assert any(
        violation.path == ("source",) and violation.keyword == "additionalProperties"
        for violation in violations
    )
