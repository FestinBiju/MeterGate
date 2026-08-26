"""Small Redis-backed fixed-window guard for high-risk operator actions."""

from typing import Protocol, runtime_checkable

from redis.asyncio import Redis

_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {count, redis.call('TTL', KEYS[1])}
"""


class OperatorRateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__("Operator action rate limit exceeded")


@runtime_checkable
class OperatorRateLimiter(Protocol):
    async def require(
        self,
        *,
        account_id: str,
        action: str,
        limit: int,
        window_seconds: int,
    ) -> None: ...


class RedisOperatorRateLimiter:
    def __init__(self, client: Redis) -> None:
        self._client = client

    async def require(
        self,
        *,
        account_id: str,
        action: str,
        limit: int,
        window_seconds: int,
    ) -> None:
        key = f"metergate:operator-rate:v1:{account_id}:{action}"
        outcome = await self._client.eval(
            _RATE_LIMIT_SCRIPT,
            1,
            key,
            window_seconds,
        )
        if not isinstance(outcome, (list, tuple)) or len(outcome) != 2:
            raise RuntimeError("Operator rate limiter returned invalid state")
        count, ttl = int(outcome[0]), int(outcome[1])
        if count > limit:
            raise OperatorRateLimitExceeded(ttl)
