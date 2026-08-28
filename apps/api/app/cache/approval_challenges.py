"""Strict Redis-backed, single-use state for WebAuthn approval ceremonies."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from app.domain.base64url import decode_base64url
from app.domain.canonical_json import CanonicalJSONError, canonical_json_bytes
from app.domain.hashing import MAX_CANONICAL_INTEGER, canonical_utc_datetime

CHALLENGE_STATE_VERSION = "1"
_CHALLENGE_ID_PATTERN = r"^ach_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_ACCOUNT_ID_PATTERN = r"^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_APPROVAL_IDENTITY_ID_PATTERN = r"^aid_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_PASSKEY_CREDENTIAL_ID_PATTERN = r"^pkc_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_POLICY_EVALUATION_ID_PATTERN = r"^pye_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_POLICY_ID_PATTERN = r"^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_QUOTE_ID_PATTERN = r"^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
_BASE64URL_PATTERN = r"^[A-Za-z0-9_-]+$"
_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9:_-]+$")
_CONSUMED_MARKER_TTL_SECONDS = 1_200

ChallengeKind = Literal["registration", "approval"]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ChallengeStoreError(RuntimeError):
    """Base class for sanitized challenge-storage failures."""

    def __init__(self, challenge_id: str, message: str) -> None:
        self.challenge_id = challenge_id
        super().__init__(message)


class ChallengeNotFoundError(ChallengeStoreError):
    def __init__(self, challenge_id: str) -> None:
        super().__init__(challenge_id, "WebAuthn challenge was not found")


class ChallengeExpiredError(ChallengeStoreError):
    def __init__(self, challenge_id: str) -> None:
        super().__init__(challenge_id, "WebAuthn challenge has expired")


class ChallengeAlreadyUsedError(ChallengeStoreError):
    def __init__(self, challenge_id: str) -> None:
        super().__init__(challenge_id, "WebAuthn challenge was already used")


class ChallengeAlreadyExistsError(ChallengeStoreError):
    def __init__(self, challenge_id: str) -> None:
        super().__init__(challenge_id, "WebAuthn challenge ID already exists")


class ChallengeStateInvalidError(ChallengeStoreError):
    def __init__(self, challenge_id: str) -> None:
        super().__init__(challenge_id, "Stored WebAuthn challenge state is invalid")


class AllowedCredentialBinding(BaseModel):
    """Issuance-time credential binding without a raw WebAuthn credential ID."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    passkey_credential_id: Annotated[str, Field(pattern=_PASSKEY_CREDENTIAL_ID_PATTERN)]
    credential_id_hash: Annotated[str, Field(pattern=_HASH_PATTERN)]


class _ChallengeState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["1"] = CHALLENGE_STATE_VERSION
    kind: ChallengeKind
    challenge_id: Annotated[str, Field(pattern=_CHALLENGE_ID_PATTERN)]
    challenge: Annotated[
        str,
        Field(min_length=43, max_length=128, pattern=_BASE64URL_PATTERN),
    ]
    issued_at: datetime
    expires_at: datetime

    @field_validator("challenge", mode="after")
    @classmethod
    def validate_challenge_entropy(cls, value: str) -> str:
        try:
            decoded = decode_base64url(value, maximum_bytes=96)
        except ValueError as error:
            raise ValueError("Challenge must be unpadded base64url") from error
        if len(decoded) < 32:
            raise ValueError("Challenge must contain at least 32 bytes")
        return value

    @field_validator("issued_at", "expires_at", mode="after")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Challenge timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_lifetime(self) -> _ChallengeState:
        if self.expires_at <= self.issued_at:
            raise ValueError("Challenge expiry must be after issuance")
        return self


class RegistrationChallengeState(_ChallengeState):
    """Redis-only binding for one passkey registration ceremony."""

    kind: Literal["registration"] = "registration"
    account_id: Annotated[str, Field(pattern=_ACCOUNT_ID_PATTERN)]
    approval_identity_id: Annotated[str, Field(pattern=_APPROVAL_IDENTITY_ID_PATTERN)]
    session_id_hash: Annotated[str, Field(pattern=_HASH_PATTERN)]
    user_handle: Annotated[
        str,
        Field(min_length=1, max_length=86, pattern=_BASE64URL_PATTERN),
    ]


class ApprovalChallengeState(_ChallengeState):
    """Redis-only binding for one exact, server-derived approval review."""

    kind: Literal["approval"] = "approval"
    account_id: Annotated[str, Field(pattern=_ACCOUNT_ID_PATTERN)]
    approval_identity_id: Annotated[str, Field(pattern=_APPROVAL_IDENTITY_ID_PATTERN)]
    evaluation_id: Annotated[str, Field(pattern=_POLICY_EVALUATION_ID_PATTERN)]
    policy_id: Annotated[str, Field(pattern=_POLICY_ID_PATTERN)]
    policy_hash: Annotated[str, Field(pattern=_HASH_PATTERN)]
    quote_id: Annotated[str, Field(pattern=_QUOTE_ID_PATTERN)]
    quote_hash: Annotated[str, Field(pattern=_HASH_PATTERN)]
    review: Annotated[dict[str, JsonValue], Field(min_length=1)]
    review_hash: Annotated[str, Field(pattern=_HASH_PATTERN)]
    amount: Annotated[int, Field(ge=0, le=MAX_CANONICAL_INTEGER)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    allowed_credentials: Annotated[
        list[AllowedCredentialBinding],
        Field(min_length=1, max_length=100),
    ]

    @field_validator("allowed_credentials", mode="after")
    @classmethod
    def validate_allowed_credentials(
        cls,
        value: list[AllowedCredentialBinding],
    ) -> list[AllowedCredentialBinding]:
        passkey_ids = [item.passkey_credential_id for item in value]
        credential_hashes = [item.credential_id_hash for item in value]
        if len(passkey_ids) != len(set(passkey_ids)) or len(credential_hashes) != len(
            set(credential_hashes)
        ):
            raise ValueError("Allowed credentials must be unique")
        return value


@runtime_checkable
class ChallengeStore(Protocol):
    """Ephemeral challenge persistence with atomic, destructive reads."""

    async def save_registration(
        self,
        state: RegistrationChallengeState,
        *,
        ttl_seconds: int,
    ) -> None: ...

    async def consume_registration(
        self,
        challenge_id: str,
        *,
        account_id: str,
    ) -> RegistrationChallengeState: ...

    async def save_approval(
        self,
        state: ApprovalChallengeState,
        *,
        ttl_seconds: int,
    ) -> None: ...

    async def consume_approval(
        self,
        challenge_id: str,
        *,
        account_id: str,
    ) -> ApprovalChallengeState: ...


@runtime_checkable
class RedisChallengeClient(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> Any: ...


_CREATE_SCRIPT = """
local state_key = KEYS[1]
local status_key = KEYS[2]
if redis.call('EXISTS', state_key) == 1 or redis.call('EXISTS', status_key) == 1 then
  return 0
end
redis.call('SET', state_key, ARGV[1], 'EX', ARGV[2])
redis.call('SET', status_key, 'issued', 'EX', ARGV[3])
return 1
"""

_CONSUME_SCRIPT = """
local state_key = KEYS[1]
local status_key = KEYS[2]
local payload = redis.call('GETDEL', state_key)
if payload then
  redis.call('SET', status_key, 'consumed', 'EX', ARGV[1])
  return {1, payload}
end
local status = redis.call('GET', status_key)
if status == 'consumed' then
  return {2, false}
end
if status == 'issued' then
  return {3, false}
end
return {0, false}
"""

StateT = TypeVar("StateT", RegistrationChallengeState, ApprovalChallengeState)


class RedisChallengeStore:
    """Redis implementation using atomic Lua around GETDEL and status tombstones."""

    def __init__(
        self,
        client: RedisChallengeClient,
        *,
        namespace: str = "metergate:v1:webauthn",
        clock: Clock = utc_now,
    ) -> None:
        if _NAMESPACE_PATTERN.fullmatch(namespace) is None:
            raise ValueError("Redis challenge namespace contains unsupported characters")
        self._client = client
        self._namespace = namespace.rstrip(":")
        self._clock = clock

    async def save_registration(
        self,
        state: RegistrationChallengeState,
        *,
        ttl_seconds: int,
    ) -> None:
        await self._save("registration", state, ttl_seconds=ttl_seconds)

    async def consume_registration(
        self,
        challenge_id: str,
        *,
        account_id: str,
    ) -> RegistrationChallengeState:
        return await self._consume(
            "registration",
            challenge_id,
            account_id,
            RegistrationChallengeState,
        )

    async def save_approval(
        self,
        state: ApprovalChallengeState,
        *,
        ttl_seconds: int,
    ) -> None:
        await self._save("approval", state, ttl_seconds=ttl_seconds)

    async def consume_approval(
        self,
        challenge_id: str,
        *,
        account_id: str,
    ) -> ApprovalChallengeState:
        return await self._consume(
            "approval",
            challenge_id,
            account_id,
            ApprovalChallengeState,
        )

    async def _save(
        self,
        kind: ChallengeKind,
        state: _ChallengeState,
        *,
        ttl_seconds: int,
    ) -> None:
        if state.kind != kind:
            raise ChallengeStateInvalidError(state.challenge_id)
        if isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= 600:
            raise ValueError("Challenge TTL must be between one and 600 seconds")
        try:
            payload_value = state.model_dump(mode="json")
            payload_value["issued_at"] = canonical_utc_datetime(state.issued_at)
            payload_value["expires_at"] = canonical_utc_datetime(state.expires_at)
            payload = canonical_json_bytes(payload_value)
        except (CanonicalJSONError, TypeError, ValueError) as error:
            raise ChallengeStateInvalidError(state.challenge_id) from error

        state_key, status_key = self._keys(kind, state.account_id, state.challenge_id)
        marker_ttl = ttl_seconds * 2
        created = await self._client.eval(
            _CREATE_SCRIPT,
            2,
            state_key,
            status_key,
            payload,
            ttl_seconds,
            marker_ttl,
        )
        if created != 1:
            raise ChallengeAlreadyExistsError(state.challenge_id)

    async def _consume(
        self,
        kind: ChallengeKind,
        challenge_id: str,
        account_id: str,
        state_type: type[StateT],
    ) -> StateT:
        self._validate_challenge_id(challenge_id)
        self._validate_account_id(account_id)
        state_key, status_key = self._keys(kind, account_id, challenge_id)
        result = await self._client.eval(
            _CONSUME_SCRIPT,
            2,
            state_key,
            status_key,
            _CONSUMED_MARKER_TTL_SECONDS,
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise ChallengeStateInvalidError(challenge_id)

        outcome, payload = result
        if outcome == 0:
            raise ChallengeNotFoundError(challenge_id)
        if outcome == 2:
            raise ChallengeAlreadyUsedError(challenge_id)
        if outcome == 3:
            raise ChallengeExpiredError(challenge_id)
        if outcome != 1 or not isinstance(payload, (bytes, str)):
            raise ChallengeStateInvalidError(challenge_id)

        try:
            state = state_type.model_validate_json(payload)
        except (ValidationError, ValueError, TypeError) as error:
            raise ChallengeStateInvalidError(challenge_id) from error
        if (
            state.challenge_id != challenge_id
            or state.kind != kind
            or state.account_id != account_id
        ):
            raise ChallengeStateInvalidError(challenge_id)
        if self._read_clock() >= state.expires_at:
            raise ChallengeExpiredError(challenge_id)
        return state

    def _keys(
        self,
        kind: ChallengeKind,
        account_id: str,
        challenge_id: str,
    ) -> tuple[str, str]:
        base = f"{self._namespace}:{kind}:{{{account_id}:{challenge_id}}}"
        return f"{base}:state", f"{base}:status"

    @staticmethod
    def _validate_challenge_id(challenge_id: str) -> None:
        if re.fullmatch(_CHALLENGE_ID_PATTERN, challenge_id) is None:
            raise ChallengeNotFoundError(challenge_id)

    @staticmethod
    def _validate_account_id(account_id: str) -> None:
        if re.fullmatch(_ACCOUNT_ID_PATTERN, account_id) is None:
            raise ChallengeNotFoundError(account_id)

    def _read_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Challenge-store clock must be timezone-aware")
        return value.astimezone(UTC)
