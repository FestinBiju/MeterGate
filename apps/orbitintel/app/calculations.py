"""Deterministic two-body orbital calculations for OrbitIntel products."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from app.schemas import (
    CalculationMetadata,
    DerivedOrbit,
    FreshnessAnalysis,
    GPSnapshot,
    HeuristicIndicators,
    SourceFields,
)

EARTH_GRAVITATIONAL_PARAMETER_KM3_S2 = 398_600.4418
EARTH_REFERENCE_RADIUS_KM = 6_378.137
SECONDS_PER_DAY = 86_400.0
MINUTES_PER_DAY = 1_440.0

FORMULAS = {
    "orbital_period_minutes": "1440 / mean_motion_rev_per_day",
    "semi_major_axis_km": "(mu / (mean_motion_rev_per_day * 2*pi / 86400)^2)^(1/3)",
    "approximate_perigee_altitude_km": "semi_major_axis_km * (1 - eccentricity) - earth_radius_km",
    "approximate_apogee_altitude_km": "semi_major_axis_km * (1 + eccentricity) - earth_radius_km",
}

APPROXIMATION_NOTES = [
    "Derived altitudes use a two-body Keplerian approximation from current GP elements.",
    "Earth is represented by the WGS 84 semi-major-axis reference radius.",
    "Atmospheric drag, perturbation propagation, and conjunction geometry are not evaluated.",
]


def derive_orbit(snapshot: GPSnapshot) -> DerivedOrbit:
    """Calculate bounded, deterministic quantities from one validated GP snapshot."""
    mean_motion_radians_per_second = snapshot.mean_motion * 2.0 * math.pi / SECONDS_PER_DAY
    semi_major_axis = (
        EARTH_GRAVITATIONAL_PARAMETER_KM3_S2
        / (mean_motion_radians_per_second * mean_motion_radians_per_second)
    ) ** (1.0 / 3.0)
    period = MINUTES_PER_DAY / snapshot.mean_motion
    perigee = semi_major_axis * (1.0 - snapshot.eccentricity) - EARTH_REFERENCE_RADIUS_KM
    apogee = semi_major_axis * (1.0 + snapshot.eccentricity) - EARTH_REFERENCE_RADIUS_KM
    return DerivedOrbit(
        orbital_period_minutes=_rounded(period, 6),
        semi_major_axis_km=_rounded(semi_major_axis, 6),
        approximate_perigee_altitude_km=_rounded(perigee, 6),
        approximate_apogee_altitude_km=_rounded(apogee, 6),
        orbital_regime=classify_orbital_regime(
            period_minutes=period,
            perigee_altitude_km=perigee,
            apogee_altitude_km=apogee,
            eccentricity=snapshot.eccentricity,
            inclination_degrees=snapshot.inclination,
        ),
    )


def classify_orbital_regime(
    *,
    period_minutes: float,
    perigee_altitude_km: float,
    apogee_altitude_km: float,
    eccentricity: float,
    inclination_degrees: float,
) -> str:
    """Classify broad regimes from derived altitudes without operational claims."""
    if perigee_altitude_km < 0:
        return "suborbital_or_decayed"
    if apogee_altitude_km < 2_000:
        return "low_earth_orbit"
    if (
        1_300 <= period_minutes <= 1_550
        and 34_500 <= perigee_altitude_km <= 37_000
        and 34_500 <= apogee_altitude_km <= 37_000
        and eccentricity <= 0.1
        and inclination_degrees <= 20
    ):
        return "geosynchronous_like"
    if eccentricity >= 0.25:
        return "highly_elliptical_orbit"
    if apogee_altitude_km < 35_786:
        return "medium_earth_orbit"
    return "high_earth_orbit"


def analyze_freshness(snapshot: GPSnapshot, *, result_time: datetime) -> FreshnessAnalysis:
    normalized_time = _as_utc(result_time)
    age_hours = (normalized_time - _as_utc(snapshot.epoch)).total_seconds() / 3_600.0
    if age_hours < -1:
        indicator = "future_epoch"
    elif age_hours <= 24:
        indicator = "current"
    elif age_hours <= 72:
        indicator = "aging"
    else:
        indicator = "stale"
    return FreshnessAnalysis(
        element_set_age_hours=_rounded(age_hours, 6),
        data_freshness_indicator=indicator,
    )


def heuristic_indicators(
    snapshot: GPSnapshot,
    derived: DerivedOrbit,
) -> HeuristicIndicators:
    eccentricity = snapshot.eccentricity
    if eccentricity < 0.01:
        eccentricity_indicator = "near_circular"
    elif eccentricity < 0.1:
        eccentricity_indicator = "low"
    elif eccentricity < 0.25:
        eccentricity_indicator = "moderate"
    else:
        eccentricity_indicator = "high"
    return HeuristicIndicators(
        eccentricity_indicator=eccentricity_indicator,
        very_low_perigee_indicator=(derived.approximate_perigee_altitude_km < 200.0),
    )


def normalized_source(snapshot: GPSnapshot) -> SourceFields:
    return SourceFields(
        norad_id=snapshot.norad_id,
        object_name=snapshot.object_name,
        international_designator=snapshot.international_designator,
        epoch=snapshot.epoch,
        mean_motion_rev_per_day=snapshot.mean_motion,
        eccentricity=snapshot.eccentricity,
        inclination_degrees=snapshot.inclination,
        raan_degrees=snapshot.raan,
        argument_of_pericenter_degrees=snapshot.argument_of_pericenter,
        mean_anomaly_degrees=snapshot.mean_anomaly,
        classification_type=snapshot.classification_type,
        element_set_number=snapshot.element_set_number,
        revolution_number_at_epoch=snapshot.revolution_number_at_epoch,
        bstar=snapshot.bstar,
        mean_motion_dot=snapshot.mean_motion_dot,
        mean_motion_ddot=snapshot.mean_motion_ddot,
    )


def calculation_metadata() -> CalculationMetadata:
    return CalculationMetadata(
        model="two_body_keplerian_approximation",
        earth_gravitational_parameter_km3_s2=(EARTH_GRAVITATIONAL_PARAMETER_KM3_S2),
        earth_reference_radius_km=EARTH_REFERENCE_RADIUS_KM,
        formulas=FORMULAS,
        approximation_notes=APPROXIMATION_NOTES,
    )


def _rounded(value: float, digits: int) -> float:
    if not math.isfinite(value):
        raise ValueError("Calculated orbital value is not finite")
    return round(value, digits)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Calculation timestamps must be timezone-aware")
    return value.astimezone(UTC)
