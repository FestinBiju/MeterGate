"""OrbitIntel service dispatch over cached, validated CelesTrak snapshots."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.cache import RedisGPSnapshotCache
from app.calculations import (
    analyze_freshness,
    calculation_metadata,
    derive_orbit,
    heuristic_indicators,
    normalized_source,
)
from app.celestrak import GPProvider
from app.errors import UnsupportedServiceError
from app.schemas import (
    DetailedOrbitalAnalysisResult,
    OrbitalRiskResult,
    OrbitInput,
    OrbitIntelResult,
    SatelliteStatusResult,
    ServiceSlug,
)

RISK_DISCLAIMER = "This is not a conjunction warning or operational collision-risk product."


class OrbitIntelService:
    def __init__(
        self,
        provider: GPProvider,
        cache: RedisGPSnapshotCache,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        service_slug: ServiceSlug,
        input_value: OrbitInput,
    ) -> OrbitIntelResult:
        snapshot = await self._cache.get_or_load(
            input_value.norad_id,
            lambda: self._provider.fetch(input_value.norad_id),
        )
        result_time = _aware_utc(self._clock())
        if service_slug == "satellite-status-lookup":
            return SatelliteStatusResult(
                norad_id=snapshot.norad_id,
                object_name=snapshot.object_name,
                international_designator=snapshot.international_designator,
                epoch=snapshot.epoch,
                mean_motion=snapshot.mean_motion,
                eccentricity=snapshot.eccentricity,
                inclination=snapshot.inclination,
                raan=snapshot.raan,
                argument_of_pericenter=snapshot.argument_of_pericenter,
                mean_anomaly=snapshot.mean_anomaly,
                fetched_at=snapshot.fetched_at,
            )

        derived = derive_orbit(snapshot)
        freshness = analyze_freshness(snapshot, result_time=result_time)
        indicators = heuristic_indicators(snapshot, derived)
        if service_slug == "orbital-risk-report":
            return OrbitalRiskResult(
                norad_id=snapshot.norad_id,
                object_name=snapshot.object_name,
                international_designator=snapshot.international_designator,
                source_epoch=snapshot.epoch,
                element_set_age_hours=freshness.element_set_age_hours,
                orbital_period_minutes=derived.orbital_period_minutes,
                semi_major_axis_km=derived.semi_major_axis_km,
                approximate_perigee_altitude_km=(derived.approximate_perigee_altitude_km),
                approximate_apogee_altitude_km=(derived.approximate_apogee_altitude_km),
                orbital_regime=derived.orbital_regime,
                eccentricity_indicator=indicators.eccentricity_indicator,
                very_low_perigee_indicator=indicators.very_low_perigee_indicator,
                data_freshness_indicator=freshness.data_freshness_indicator,
                disclaimer=RISK_DISCLAIMER,
                fetched_at=snapshot.fetched_at,
                result_timestamp=result_time,
            )
        if service_slug == "detailed-orbital-analysis":
            return DetailedOrbitalAnalysisResult(
                source=normalized_source(snapshot),
                derived=derived,
                freshness=freshness,
                heuristic_indicators=indicators,
                calculation_metadata=calculation_metadata(),
                disclaimer=RISK_DISCLAIMER,
                source_timestamp=snapshot.epoch,
                fetched_at=snapshot.fetched_at,
                result_timestamp=result_time,
            )
        raise UnsupportedServiceError(
            "OrbitIntel does not support this service",
            "ORBITINTEL_SERVICE_UNSUPPORTED",
        )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("OrbitIntel result clock must be timezone-aware")
    return value.astimezone(UTC)
