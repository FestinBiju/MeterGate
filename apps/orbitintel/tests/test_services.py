from datetime import UTC, datetime

import pytest

from app.cache import RedisGPSnapshotCache
from app.schemas import (
    DetailedOrbitalAnalysisResult,
    OrbitalRiskResult,
    OrbitInput,
    SatelliteStatusResult,
)
from app.services import RISK_DISCLAIMER, OrbitIntelService
from tests.fakes import FakeGPProvider, FakeRedis


def make_service(
    redis: FakeRedis,
    provider: FakeGPProvider,
) -> OrbitIntelService:
    cache = RedisGPSnapshotCache(
        redis,
        ttl_seconds=180,
        lock_ttl_ms=30_000,
        wait_seconds=1,
        poll_seconds=0.01,
    )
    return OrbitIntelService(
        provider,
        cache,
        clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_all_services_use_one_cached_snapshot(
    fake_redis: FakeRedis,
    fake_provider: FakeGPProvider,
) -> None:
    service = make_service(fake_redis, fake_provider)
    input_value = OrbitInput(norad_id=25544)

    status = await service.execute("satellite-status-lookup", input_value)
    risk = await service.execute("orbital-risk-report", input_value)
    detailed = await service.execute("detailed-orbital-analysis", input_value)

    assert isinstance(status, SatelliteStatusResult)
    assert status.object_name == "ISS (ZARYA)"
    assert status.data_source == "CelesTrak"
    assert isinstance(risk, OrbitalRiskResult)
    assert risk.assessment_scope == "heuristic_orbital_condition"
    assert risk.operational_collision_assessment is False
    assert risk.disclaimer == RISK_DISCLAIMER
    assert isinstance(detailed, DetailedOrbitalAnalysisResult)
    assert detailed.source.mean_motion_rev_per_day == 15.5
    assert detailed.calculation_metadata.model == "two_body_keplerian_approximation"
    assert detailed.operational_collision_assessment is False
    assert fake_provider.calls == [25544]
