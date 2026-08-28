"""Minimal Redis connectivity probe."""

from collections.abc import Callable
from typing import Protocol

from redis.asyncio import Redis


class RedisClient(Protocol):
    async def ping(self) -> bool: ...

    async def aclose(self) -> None: ...


RedisClientFactory = Callable[[str], RedisClient]


def create_redis_client(redis_url: str) -> RedisClient:
    """Create a short-lived Redis client for a readiness probe."""
    return Redis.from_url(redis_url, decode_responses=True)


async def check_redis(
    redis_url: str,
    *,
    client_factory: RedisClientFactory = create_redis_client,
) -> None:
    """Ping Redis and always release the client connection resources."""
    client = client_factory(redis_url)
    try:
        if await client.ping() is not True:
            raise RuntimeError("Redis returned an unexpected PING response")
    finally:
        await client.aclose()
