"""Infrastructure readiness orchestration."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from time import perf_counter

from app.cache.redis import check_redis
from app.core.config import Settings
from app.db.postgresql import check_postgresql
from app.schemas.health import (
    ComponentReadiness,
    DependencyStatus,
    ReadinessComponents,
    ReadinessResponse,
)

logger = logging.getLogger(__name__)
Probe = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ReadinessService:
    """Run independent dependency probes with a shared time bound."""

    postgresql_probe: Probe
    redis_probe: Probe
    timeout_seconds: float

    async def check(self, *, service_name: str) -> ReadinessResponse:
        postgresql, redis = await asyncio.gather(
            self._check_component("postgresql", self.postgresql_probe),
            self._check_component("redis", self.redis_probe),
        )
        overall_status = (
            DependencyStatus.OK
            if postgresql.status is DependencyStatus.OK and redis.status is DependencyStatus.OK
            else DependencyStatus.UNAVAILABLE
        )
        return ReadinessResponse(
            status=overall_status,
            service=service_name,
            components=ReadinessComponents(postgresql=postgresql, redis=redis),
        )

    async def _check_component(self, component_name: str, probe: Probe) -> ComponentReadiness:
        started_at = perf_counter()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await probe()
        except Exception as error:
            logger.warning(
                "Readiness probe failed: component=%s error_type=%s",
                component_name,
                type(error).__name__,
            )
            return ComponentReadiness(
                status=DependencyStatus.UNAVAILABLE,
                detail="unreachable",
                latency_ms=_elapsed_ms(started_at),
            )
        return ComponentReadiness(
            status=DependencyStatus.OK,
            detail="reachable",
            latency_ms=_elapsed_ms(started_at),
        )


def build_readiness_service(settings: Settings) -> ReadinessService:
    """Bind validated settings to real PostgreSQL and Redis probes."""
    return ReadinessService(
        postgresql_probe=partial(
            check_postgresql,
            settings.database_url.get_secret_value(),
        ),
        redis_probe=partial(check_redis, settings.redis_url.get_secret_value()),
        timeout_seconds=settings.healthcheck_timeout_seconds,
    )


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1_000, 2)
