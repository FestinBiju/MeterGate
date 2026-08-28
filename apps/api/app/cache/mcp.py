"""Small Redis-backed limiter for scoped MCP tool calls."""

from typing import Protocol, runtime_checkable

from redis.asyncio import Redis
from redis.exceptions import RedisError

_RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {current, redis.call('TTL', KEYS[1])}
"""


class McpRateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__("MCP tool rate limit exceeded")


class McpRateLimiterUnavailable(RuntimeError):
    """Raised when the fail-closed Redis authority cannot be reached."""


@runtime_checkable
class McpRateLimiter(Protocol):
    """Fail-closed authority for one scoped buyer-agent tool invocation."""

    async def require(
        self,
        *,
        account_id: str,
        agent_session_id: str,
        action: str,
        limit: int,
        window_seconds: int,
    ) -> None: ...


class RedisMcpRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def require(
        self,
        *,
        account_id: str,
        agent_session_id: str,
        action: str,
        limit: int,
        window_seconds: int,
    ) -> None:
        key = f"metergate:mcp-rate-limit:v1:{account_id}:{agent_session_id}:{action}"
        try:
            result = await self._redis.eval(_RATE_LIMIT_SCRIPT, 1, key, window_seconds)
        except RedisError as error:
            raise McpRateLimiterUnavailable("MCP rate limiter is unavailable") from error
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise McpRateLimiterUnavailable("MCP rate limiter returned invalid state")
        try:
            count, ttl = int(result[0]), int(result[1])
        except (TypeError, ValueError) as error:
            raise McpRateLimiterUnavailable("MCP rate limiter returned invalid state") from error
        if count > limit:
            raise McpRateLimitExceeded(ttl if ttl > 0 else window_seconds)
