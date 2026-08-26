import os
from datetime import UTC, datetime

import httpx2
import pytest

from app.celestrak import CelesTrakClient


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_CELESTRAK_INTEGRATION") != "1",
    reason="Set RUN_CELESTRAK_INTEGRATION=1 to call live CelesTrak",
)
async def test_live_iss_gp_record_has_expected_stable_shape() -> None:
    async with httpx2.AsyncClient(
        timeout=httpx2.Timeout(connect=5, read=10, write=5, pool=5),
        follow_redirects=False,
        headers={
            "Accept": "application/json",
            "User-Agent": "OrbitIntel/0.1 live integration check (MeterGate development)",
        },
    ) as http_client:
        client = CelesTrakClient(
            http_client,
            maximum_response_bytes=65_536,
            maximum_attempts=2,
            retry_base_delay_seconds=0.25,
            request_timeout_seconds=15,
            clock=lambda: datetime.now(UTC),
        )
        snapshot = await client.fetch(25544)

    assert snapshot.norad_id == 25544
    assert snapshot.object_name
    assert snapshot.mean_motion > 0
    assert 0 <= snapshot.eccentricity < 1
    assert snapshot.epoch.tzinfo is not None
    assert snapshot.fetched_at.tzinfo is not None
