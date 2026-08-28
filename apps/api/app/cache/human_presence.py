"""Redis-only, atomic one-time WebAuthn state for proof of human presence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from app.cache.approval_challenges import (
    AllowedCredentialBinding,
    ChallengeAlreadyExistsError,
    ChallengeAlreadyUsedError,
    ChallengeExpiredError,
    ChallengeNotFoundError,
    ChallengeStateInvalidError,
)
from app.domain.canonical_json import canonical_json_bytes


class HumanPresenceChallengeState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["1"] = "1"
    challenge_id: Annotated[str, Field(pattern=r"^hpc_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
    challenge: Annotated[str, Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
    account_id: Annotated[str, Field(pattern=r"^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
    session_id_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    action_class: Literal["new_agent_session"]
    resource_binding: dict[str, JsonValue]
    resource_binding_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    origin: Annotated[str, Field(min_length=1, max_length=500)]
    approval_identity_id: Annotated[str, Field(pattern=r"^aid_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
    allowed_credentials: Annotated[
        list[AllowedCredentialBinding], Field(min_length=1, max_length=100)
    ]
    issued_at: datetime
    expires_at: datetime


@runtime_checkable
class HumanPresenceStore(Protocol):
    async def save(self, state: HumanPresenceChallengeState, *, ttl_seconds: int) -> None: ...
    async def consume(
        self, challenge_id: str, *, account_id: str
    ) -> HumanPresenceChallengeState: ...


_CREATE = """
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('SET', KEYS[2], 'issued', 'EX', ARGV[3])
return 1
"""
_CONSUME = """
local payload = redis.call('GETDEL', KEYS[1])
if payload then redis.call('SET', KEYS[2], 'consumed', 'EX', ARGV[1]); return {1, payload} end
local status = redis.call('GET', KEYS[2])
if status == 'consumed' then return {2, false} end
if status == 'issued' then return {3, false} end
return {0, false}
"""


class RedisHumanPresenceStore:
    def __init__(self, client: Any, *, namespace: str = "metergate:v1:human-presence") -> None:
        self._client = client
        self._namespace = namespace

    async def save(self, state: HumanPresenceChallengeState, *, ttl_seconds: int) -> None:
        if not 1 <= ttl_seconds <= 600:
            raise ValueError("Human-presence challenge TTL must be 1-600 seconds")
        state_key, status_key = self._keys(state.account_id, state.challenge_id)
        created = await self._client.eval(
            _CREATE,
            2,
            state_key,
            status_key,
            canonical_json_bytes(state.model_dump(mode="json")),
            ttl_seconds,
            ttl_seconds * 2,
        )
        if created != 1:
            raise ChallengeAlreadyExistsError(state.challenge_id)

    async def consume(self, challenge_id: str, *, account_id: str) -> HumanPresenceChallengeState:
        state_key, status_key = self._keys(account_id, challenge_id)
        result = await self._client.eval(_CONSUME, 2, state_key, status_key, 1_200)
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise ChallengeStateInvalidError(challenge_id)
        outcome, payload = result
        if outcome == 0:
            raise ChallengeNotFoundError(challenge_id)
        if outcome == 2:
            raise ChallengeAlreadyUsedError(challenge_id)
        if outcome == 3:
            raise ChallengeExpiredError(challenge_id)
        try:
            state = HumanPresenceChallengeState.model_validate_json(payload)
        except (ValidationError, TypeError, ValueError) as error:
            raise ChallengeStateInvalidError(challenge_id) from error
        now = datetime.now(UTC)
        expires = (
            state.expires_at if state.expires_at.tzinfo else state.expires_at.replace(tzinfo=UTC)
        )
        if state.account_id != account_id or state.challenge_id != challenge_id:
            raise ChallengeStateInvalidError(challenge_id)
        if now >= expires:
            raise ChallengeExpiredError(challenge_id)
        return state

    def _keys(self, account_id: str, challenge_id: str) -> tuple[str, str]:
        base = f"{self._namespace}:{{{account_id}:{challenge_id}}}"
        return f"{base}:state", f"{base}:status"
