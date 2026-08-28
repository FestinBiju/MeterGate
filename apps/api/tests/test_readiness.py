import asyncio
from collections.abc import Callable

from fastapi.testclient import TestClient


async def healthy_probe() -> None:
    return None


async def failing_probe() -> None:
    raise ConnectionError("an internal error that must not reach the response")


def test_readiness_returns_component_statuses(make_client: Callable[..., TestClient]) -> None:
    response = make_client().get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "metergate-api"
    assert body["components"]["postgresql"]["status"] == "ok"
    assert body["components"]["postgresql"]["detail"] == "reachable"
    assert body["components"]["redis"]["status"] == "ok"
    assert body["components"]["redis"]["detail"] == "reachable"
    assert body["components"]["postgresql"]["latency_ms"] >= 0
    assert body["components"]["redis"]["latency_ms"] >= 0


def test_readiness_returns_503_and_sanitizes_probe_failure(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client(postgresql_probe=failing_probe).get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["components"]["postgresql"]["status"] == "unavailable"
    assert body["components"]["postgresql"]["detail"] == "unreachable"
    assert body["components"]["redis"]["status"] == "ok"
    assert "internal error" not in response.text


def test_readiness_returns_503_when_redis_fails(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client(redis_probe=failing_probe).get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["components"]["postgresql"]["status"] == "ok"
    assert body["components"]["redis"]["status"] == "unavailable"
    assert body["components"]["redis"]["detail"] == "unreachable"
    assert "internal error" not in response.text


def test_readiness_times_out_a_slow_probe(make_client: Callable[..., TestClient]) -> None:
    async def slow_probe() -> None:
        await asyncio.sleep(0.1)

    response = make_client(redis_probe=slow_probe, timeout_seconds=0.001).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["components"]["redis"]["status"] == "unavailable"


def test_readiness_times_out_a_slow_postgresql_probe(
    make_client: Callable[..., TestClient],
) -> None:
    async def slow_probe() -> None:
        await asyncio.sleep(0.1)

    response = make_client(postgresql_probe=slow_probe, timeout_seconds=0.001).get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["components"]["postgresql"]["status"] == "unavailable"
    assert body["components"]["postgresql"]["detail"] == "unreachable"
    assert body["components"]["redis"]["status"] == "ok"


def test_readiness_returns_503_when_both_dependencies_fail(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client(
        postgresql_probe=failing_probe,
        redis_probe=failing_probe,
    ).get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["components"]["postgresql"]["status"] == "unavailable"
    assert body["components"]["redis"]["status"] == "unavailable"
    assert "internal error" not in response.text
