"""Small deterministic test doubles for Redis and the GP provider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from redis.exceptions import RedisError

from app.schemas import GPSnapshot


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, bytes, int | None, int | None, bool]] = []
        self.fail = False
        self._mutex = asyncio.Lock()

    async def get(self, name: str) -> bytes | None:
        if self.fail:
            raise RedisError("forced redis failure")
        async with self._mutex:
            return self.values.get(name)

    async def set(
        self,
        name: str,
        value: bytes | str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        if self.fail:
            raise RedisError("forced redis failure")
        normalized = value.encode("utf-8") if isinstance(value, str) else value
        async with self._mutex:
            self.set_calls.append((name, normalized, ex, px, nx))
            if nx and name in self.values:
                return None
            self.values[name] = normalized
            return True

    async def delete(self, *names: str) -> int:
        if self.fail:
            raise RedisError("forced redis failure")
        async with self._mutex:
            deleted = 0
            for name in names:
                if name in self.values:
                    deleted += 1
                    del self.values[name]
            return deleted

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        del script
        if self.fail:
            raise RedisError("forced redis failure")
        async with self._mutex:
            if numkeys == 1:
                key = str(keys_and_args[0])
                token = _as_bytes(keys_and_args[1])
                if self.values.get(key) == token:
                    del self.values[key]
                    return 1
                return 0
            if numkeys == 2:
                lock_key = str(keys_and_args[0])
                started_key = str(keys_and_args[1])
                token = _as_bytes(keys_and_args[2])
                retry_safe = str(keys_and_args[3]) == "1"
                if self.values.get(lock_key) != token:
                    return 0
                if retry_safe and self.values.get(started_key) == token:
                    del self.values[started_key]
                del self.values[lock_key]
                return 1
            if numkeys == 4:
                lock_key = str(keys_and_args[0])
                result_key = str(keys_and_args[1])
                binding_key = str(keys_and_args[2])
                started_key = str(keys_and_args[3])
                token = _as_bytes(keys_and_args[4])
                result = _as_bytes(keys_and_args[5])
                request_hash = _as_bytes(keys_and_args[7])
                if self.values.get(binding_key) != request_hash:
                    return -1
                if self.values.get(lock_key) != token or self.values.get(started_key) != token:
                    return 0
                self.values[result_key] = result
                del self.values[started_key]
                del self.values[lock_key]
                return 1
            raise AssertionError(f"Unexpected Lua key count: {numkeys}")


class FakeGPProvider:
    def __init__(
        self,
        snapshot: GPSnapshot,
        *,
        error_factory: Callable[[], Exception] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error_factory = error_factory
        self.calls: list[int] = []

    async def fetch(self, norad_id: int) -> GPSnapshot:
        self.calls.append(norad_id)
        if self.error_factory is not None:
            raise self.error_factory()
        return self.snapshot


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError("Fake Redis accepts only bytes or strings")
