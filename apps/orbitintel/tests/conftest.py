from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.application import create_app
from app.core.config import Settings
from app.schemas import GPSnapshot
from tests.fakes import FakeGPProvider, FakeRedis

INTERNAL_TOKEN = "orbitintel-test-token-000000000000000000000000"
FULFILLMENT_ID = "ful_01J00000000000000000000000"
SECOND_FULFILLMENT_ID = "ful_01J00000000000000000000001"
THIRD_FULFILLMENT_ID = "ful_01J00000000000000000000002"
SERVICE_ID = "svc_01J00000000000000000000000"
RESULT_TIME = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        orbitintel_environment="test",
        orbitintel_shared_secret=INTERNAL_TOKEN,
        redis_url="redis://127.0.0.1:6379/15",
        _env_file=None,
    )


@pytest.fixture
def snapshot() -> GPSnapshot:
    return GPSnapshot(
        norad_id=25544,
        object_name="ISS (ZARYA)",
        international_designator="1998-067A",
        epoch=datetime(2026, 8, 26, 6, 0, tzinfo=UTC),
        mean_motion=15.5,
        eccentricity=0.0005,
        inclination=51.64,
        raan=122.5,
        argument_of_pericenter=73.2,
        mean_anomaly=10.4,
        classification_type="U",
        element_set_number=999,
        revolution_number_at_epoch=52345,
        bstar=0.000123,
        mean_motion_dot=0.0001,
        mean_motion_ddot=0.0,
        fetched_at=datetime(2026, 8, 26, 11, 59, tzinfo=UTC),
    )


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def fake_provider(snapshot: GPSnapshot) -> FakeGPProvider:
    return FakeGPProvider(snapshot)


@pytest.fixture
def make_client(
    settings: Settings,
    fake_redis: FakeRedis,
    fake_provider: FakeGPProvider,
) -> Callable[..., TestClient]:
    clients: list[TestClient] = []

    def factory(
        *,
        effective_settings: Settings | None = None,
        redis: FakeRedis | None = None,
        provider: FakeGPProvider | None = None,
    ) -> TestClient:
        client = TestClient(
            create_app(
                settings=effective_settings or settings,
                redis_client=redis or fake_redis,
                gp_provider=provider or fake_provider,
                clock=lambda: RESULT_TIME,
            )
        )
        clients.append(client)
        return client

    yield factory

    for client in clients:
        client.close()
