"""Redis-backed execution binding and exact successful-result replay."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hmac import compare_digest

from redis.exceptions import RedisError

from app.cache import RedisClient
from app.errors import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyIntegrityError,
    IdempotencyUncertainError,
)

_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_COMPLETE_SCRIPT = """
if redis.call('GET', KEYS[3]) ~= ARGV[4] then
  return -1
end
if redis.call('GET', KEYS[1]) ~= ARGV[1]
   or redis.call('GET', KEYS[4]) ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
redis.call('EXPIRE', KEYS[3], ARGV[3])
redis.call('DEL', KEYS[4])
redis.call('DEL', KEYS[1])
return 1
"""

_ABANDON_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return 0
end
if ARGV[2] == '1' and redis.call('GET', KEYS[2]) == ARGV[1] then
  redis.call('DEL', KEYS[2])
end
redis.call('DEL', KEYS[1])
return 1
"""


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    fulfillment_execution_id: str
    request_hash: str
    token: str


@dataclass(frozen=True, slots=True)
class ReplayedExecution:
    response_body: bytes


class RedisExecutionIdempotency:
    def __init__(
        self,
        redis: RedisClient,
        *,
        ttl_seconds: int,
        lock_ttl_seconds: int,
        wait_seconds: float,
        poll_seconds: float,
        maximum_result_bytes: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._lock_ttl_seconds = lock_ttl_seconds
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds
        self._maximum_result_bytes = maximum_result_bytes
        self._sleep = sleep
        self._monotonic = monotonic

    async def claim(
        self,
        fulfillment_execution_id: str,
        request_hash: str,
    ) -> ExecutionClaim | ReplayedExecution:
        binding_key = self._binding_key(fulfillment_execution_id)
        try:
            existing_binding = await self._redis.get(binding_key)
            if (
                existing_binding is None
                and await self._redis.get(self._result_key(fulfillment_execution_id)) is not None
            ):
                raise IdempotencyIntegrityError(
                    "Persisted OrbitIntel result has no request binding",
                    "ORBITINTEL_IDEMPOTENCY_INTEGRITY_FAILED",
                )
            await self._redis.set(
                binding_key,
                request_hash,
                ex=self._ttl_seconds,
                nx=True,
            )
            persisted_binding = await self._redis.get(binding_key)
        except RedisError as error:
            raise self._unavailable() from error
        normalized_binding = _as_text(persisted_binding)
        if normalized_binding is None:
            raise self._unavailable()
        if not compare_digest(normalized_binding, request_hash):
            raise IdempotencyConflictError(
                "Fulfillment execution ID is already bound to a different request",
                "ORBITINTEL_IDEMPOTENCY_MISMATCH",
            )

        replay = await self._get_replay(fulfillment_execution_id)
        if replay is not None:
            return replay

        deadline = self._monotonic() + self._wait_seconds
        lock_key = self._lock_key(fulfillment_execution_id)
        while True:
            token = secrets.token_urlsafe(24)
            try:
                acquired = await self._redis.set(
                    lock_key,
                    token,
                    ex=self._lock_ttl_seconds,
                    nx=True,
                )
            except RedisError as error:
                raise self._unavailable() from error
            if acquired:
                replay = await self._get_replay(fulfillment_execution_id)
                if replay is not None:
                    await self._release(lock_key, token)
                    return replay
                try:
                    started = await self._redis.set(
                        self._started_key(fulfillment_execution_id),
                        token,
                        ex=self._ttl_seconds,
                        nx=True,
                    )
                except RedisError as error:
                    raise self._unavailable() from error
                if not started:
                    await self._release(lock_key, token)
                    raise IdempotencyUncertainError(
                        "This fulfillment execution has an incomplete prior attempt",
                        "ORBITINTEL_EXECUTION_UNCERTAIN",
                    )
                return ExecutionClaim(
                    fulfillment_execution_id=fulfillment_execution_id,
                    request_hash=request_hash,
                    token=token,
                )
            replay = await self._get_replay(fulfillment_execution_id)
            if replay is not None:
                return replay
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise IdempotencyInProgressError(
                    "This fulfillment execution is already in progress",
                    "ORBITINTEL_EXECUTION_IN_PROGRESS",
                )
            await self._sleep(min(self._poll_seconds, remaining))

    async def complete(self, claim: ExecutionClaim, response_body: bytes) -> None:
        if not 1 <= len(response_body) <= self._maximum_result_bytes:
            raise IdempotencyIntegrityError(
                "OrbitIntel result exceeds its persistence boundary",
                "ORBITINTEL_RESULT_SIZE_INVALID",
            )
        try:
            completed = await self._redis.eval(
                _COMPLETE_SCRIPT,
                4,
                self._lock_key(claim.fulfillment_execution_id),
                self._result_key(claim.fulfillment_execution_id),
                self._binding_key(claim.fulfillment_execution_id),
                self._started_key(claim.fulfillment_execution_id),
                claim.token,
                response_body,
                str(self._ttl_seconds),
                claim.request_hash,
            )
        except RedisError as error:
            raise self._unavailable() from error
        completion_code = int(completed or 0)
        if completion_code == -1:
            raise IdempotencyIntegrityError(
                "OrbitIntel request binding changed before result persistence",
                "ORBITINTEL_IDEMPOTENCY_INTEGRITY_FAILED",
            )
        if completion_code != 1:
            raise IdempotencyIntegrityError(
                "OrbitIntel lost ownership before the result was persisted",
                "ORBITINTEL_EXECUTION_LEASE_LOST",
            )

    async def abandon(self, claim: ExecutionClaim, *, retry_safe: bool) -> None:
        try:
            await self._redis.eval(
                _ABANDON_SCRIPT,
                2,
                self._lock_key(claim.fulfillment_execution_id),
                self._started_key(claim.fulfillment_execution_id),
                claim.token,
                "1" if retry_safe else "0",
            )
        except RedisError as error:
            raise self._unavailable() from error

    async def _get_replay(self, fulfillment_execution_id: str) -> ReplayedExecution | None:
        try:
            raw = await self._redis.get(self._result_key(fulfillment_execution_id))
        except RedisError as error:
            raise self._unavailable() from error
        if raw is None:
            return None
        body = _as_bytes(raw)
        if body is None or not 1 <= len(body) <= self._maximum_result_bytes:
            raise IdempotencyIntegrityError(
                "Persisted OrbitIntel result is invalid",
                "ORBITINTEL_IDEMPOTENCY_INTEGRITY_FAILED",
            )
        return ReplayedExecution(response_body=body)

    async def _release(self, key: str, token: str) -> None:
        try:
            await self._redis.eval(_RELEASE_LOCK_SCRIPT, 1, key, token)
        except RedisError as error:
            raise self._unavailable() from error

    @staticmethod
    def _binding_key(execution_id: str) -> str:
        return f"orbitintel:idempotency:binding:v1:{execution_id}"

    @staticmethod
    def _result_key(execution_id: str) -> str:
        return f"orbitintel:idempotency:result:v1:{execution_id}"

    @staticmethod
    def _lock_key(execution_id: str) -> str:
        return f"orbitintel:idempotency:lock:v1:{execution_id}"

    @staticmethod
    def _started_key(execution_id: str) -> str:
        return f"orbitintel:idempotency:started:v1:{execution_id}"

    @staticmethod
    def _unavailable() -> IdempotencyIntegrityError:
        return IdempotencyIntegrityError(
            "OrbitIntel idempotency storage is unavailable",
            "ORBITINTEL_IDEMPOTENCY_UNAVAILABLE",
        )


def _as_text(value: bytes | str | None) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("ascii")
        except UnicodeDecodeError:
            return None
    return None


def _as_bytes(value: bytes | str | None) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return None
