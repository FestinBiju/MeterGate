from datetime import UTC, datetime

import pytest

from app.calculations import (
    EARTH_GRAVITATIONAL_PARAMETER_KM3_S2,
    EARTH_REFERENCE_RADIUS_KM,
    analyze_freshness,
    calculation_metadata,
    classify_orbital_regime,
    derive_orbit,
    heuristic_indicators,
)
from app.schemas import GPSnapshot


def test_iss_like_reference_calculations(snapshot: GPSnapshot) -> None:
    derived = derive_orbit(snapshot)

    assert derived.orbital_period_minutes == pytest.approx(92.903226, abs=1e-6)
    assert derived.semi_major_axis_km == pytest.approx(6794.863, abs=0.01)
    assert derived.approximate_perigee_altitude_km == pytest.approx(413.329, abs=0.02)
    assert derived.approximate_apogee_altitude_km == pytest.approx(420.124, abs=0.02)
    assert derived.orbital_regime == "low_earth_orbit"


def test_freshness_and_heuristics_are_deterministic(snapshot: GPSnapshot) -> None:
    derived = derive_orbit(snapshot)
    freshness = analyze_freshness(
        snapshot,
        result_time=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )
    indicators = heuristic_indicators(snapshot, derived)

    assert freshness.element_set_age_hours == 6
    assert freshness.data_freshness_indicator == "current"
    assert indicators.eccentricity_indicator == "near_circular"
    assert indicators.very_low_perigee_indicator is False


def test_regime_classifier_covers_geo_and_high_eccentricity() -> None:
    assert (
        classify_orbital_regime(
            period_minutes=1_436,
            perigee_altitude_km=35_780,
            apogee_altitude_km=35_792,
            eccentricity=0.0002,
            inclination_degrees=0.1,
        )
        == "geosynchronous_like"
    )
    assert (
        classify_orbital_regime(
            period_minutes=700,
            perigee_altitude_km=500,
            apogee_altitude_km=40_000,
            eccentricity=0.7,
            inclination_degrees=63.4,
        )
        == "highly_elliptical_orbit"
    )


def test_calculation_metadata_names_constants_and_formulas() -> None:
    metadata = calculation_metadata()

    assert metadata.earth_gravitational_parameter_km3_s2 == (EARTH_GRAVITATIONAL_PARAMETER_KM3_S2)
    assert metadata.earth_reference_radius_km == EARTH_REFERENCE_RADIUS_KM
    assert "semi_major_axis_km" in metadata.formulas
    notes = " ".join(metadata.approximation_notes).lower()
    assert "approximation" in notes
    assert "not evaluated" in notes
