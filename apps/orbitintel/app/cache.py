"""Redis GP snapshot cache with distributed token-lock single-flight."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import ValidationError
from redis.exceptions import RedisError

from app.errors import CacheUnavailableError
from app.schemas import GPSnapshot

_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisClient(Protocol):
    async def get(self, name: str) -> bytes | str | None: ...

    async def set(
        self,
        name: str,
        value: bytes | str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> object: ...

    async def delete(self, *names: str) -> int: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...


class RedisGPSnapshotCache:
    def __init__(
        self,
        redis: RedisClient,
        *,
        ttl_seconds: int,
        lock_ttl_ms: int,
        wait_seconds: float,
        poll_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._lock_ttl_ms = lock_ttl_ms
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    async def get_or_load(
        self,
        norad_id: int,
        loader: Callable[[], Awaitable[GPSnapshot]],
    ) -> GPSnapshot:
        cached = await self._get(norad_id)
        if cached is not None:
            return cached
        deadline = self._monotonic() + self._wait_seconds
        lock_key = self._lock_key(norad_id)
        while True:
            token = secrets.token_urlsafe(24)
            acquired = await self._set_lock(lock_key, token)
            if acquired:
                try:
                    cached = await self._get(norad_id)
                    if cached is not None:
                        return cached
                    snapshot = await loader()
                    if snapshot.norad_id != norad_id:
                        raise ValueError("GP loader returned a different NORAD catalog ID")
                    await self._store(snapshot)
                    return snapshot
                finally:
                    await self._release_lock(lock_key, token)
            cached = await self._get(norad_id)
            if cached is not None:
                return cached
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise CacheUnavailableError(
                    "Timed out waiting for the shared GP snapshot loader",
                    "ORBITINTEL_GP_SINGLEFLIGHT_TIMEOUT",
                )
            await self._sleep(min(self._poll_seconds, remaining))

    async def _get(self, norad_id: int) -> GPSnapshot | None:
        key = self._cache_key(norad_id)
        try:
            raw = await self._redis.get(key)
        except RedisError as error:
            raise CacheUnavailableError(
                "OrbitIntel GP cache is unavailable",
                "ORBITINTEL_CACHE_UNAVAILABLE",
            ) from error
        if raw is None:
            return None
        try:
            snapshot = GPSnapshot.model_validate_json(raw)
        except (ValidationError, ValueError, TypeError):
            try:
                await self._redis.delete(key)
            except RedisError as error:
                raise CacheUnavailableError(
                    "OrbitIntel GP cache is unavailable",
                    "ORBITINTEL_CACHE_UNAVAILABLE",
                ) from error
            return None
        if snapshot.norad_id != norad_id:
            try:
                await self._redis.delete(key)
            except RedisError as error:
                raise CacheUnavailableError(
                    "OrbitIntel GP cache is unavailable",
                    "ORBITINTEL_CACHE_UNAVAILABLE",
                ) from error
            return None
        return snapshot

    async def _store(self, snapshot: GPSnapshot) -> None:
        payload = snapshot.model_dump_json(exclude_none=True).encode("utf-8")
        try:
            stored = await self._redis.set(
                self._cache_key(snapshot.norad_id),
                payload,
                ex=self._ttl_seconds,
            )
        except RedisError as error:
            raise CacheUnavailableError(
                "OrbitIntel GP cache is unavailable",
                "ORBITINTEL_CACHE_UNAVAILABLE",
            ) from error
        if not stored:
            raise CacheUnavailableError(
                "OrbitIntel could not persist the GP cache entry",
                "ORBITINTEL_CACHE_UNAVAILABLE",
            )

    async def _set_lock(self, key: str, token: str) -> bool:
        try:
            result = await self._redis.set(
                key,
                token,
                px=self._lock_ttl_ms,
                nx=True,
            )
        except RedisError as error:
            raise CacheUnavailableError(
                "OrbitIntel GP cache is unavailable",
                "ORBITINTEL_CACHE_UNAVAILABLE",
            ) from error
        return bool(result)

    async def _release_lock(self, key: str, token: str) -> None:
        try:
            await self._redis.eval(_RELEASE_LOCK_SCRIPT, 1, key, token)
        except RedisError as error:
            raise CacheUnavailableError(
                "OrbitIntel GP cache lock could not be released safely",
                "ORBITINTEL_CACHE_UNAVAILABLE",
            ) from error

    @staticmethod
    def _cache_key(norad_id: int) -> str:
        return f"orbitintel:celestrak:gp:v1:{norad_id}"

    @staticmethod
    def _lock_key(norad_id: int) -> str:
        return f"orbitintel:celestrak:gp-lock:v1:{norad_id}"
