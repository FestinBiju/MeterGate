from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.application import create_app
from app.core.config import Settings
from app.services.readiness import Probe, ReadinessService


async def healthy_probe() -> None:
    return None


@pytest.fixture
def settings() -> Settings:
    return Settings(
        service_name="metergate-api",
        log_level="INFO",
        healthcheck_timeout_seconds=2.0,
        database_url="postgresql://test-user:test-password@localhost:5432/metergate_test",
        redis_url="redis://localhost:6379/15",
        cors_allowed_origins=["http://localhost:3000"],
        _env_file=None,
    )


@pytest.fixture
def make_client(settings: Settings) -> Callable[..., TestClient]:
    clients: list[TestClient] = []

    def factory(
        *,
        postgresql_probe: Probe = healthy_probe,
        redis_probe: Probe = healthy_probe,
        timeout_seconds: float = 0.1,
    ) -> TestClient:
        readiness_service = ReadinessService(
            postgresql_probe=postgresql_probe,
            redis_probe=redis_probe,
            timeout_seconds=timeout_seconds,
        )
        client = TestClient(create_app(settings=settings, readiness_service=readiness_service))
        clients.append(client)
        return client

    yield factory

    for client in clients:
        client.close()
