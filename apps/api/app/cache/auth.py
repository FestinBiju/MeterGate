"""Versioned Redis state for passkey signup, login, and opaque sessions."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.domain.base64url import decode_base64url
from app.domain.canonical_json import CanonicalJSONError, canonical_json_bytes
from app.domain.hashing import canonical_utc_datetime

AUTH_STATE_VERSION = "1"
_CHALLENGE_ID_PATTERN = r"^ach_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_ACCOUNT_ID_PATTERN = r"^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_APPROVAL_IDENTITY_ID_PATTERN = r"^aid_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_PASSKEY_CREDENTIAL_ID_PATTERN = r"^pkc_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_OPAQUE_SECRET_BODY = r"[A-Za-z0-9_-]{43}"
_OPAQUE_SECRET_PATTERN = rf"^{_OPAQUE_SECRET_BODY}$"
_SESSION_ID_PATTERN = rf"^ses_{_OPAQUE_SECRET_BODY}$"
_BASE64URL_PATTERN = r"^[A-Za-z0-9_-]+$"
_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9:_-]+$")
_CONSUMED_MARKER_TTL_SECONDS = 1_200

AuthChallengeKind = Literal["signup", "login"]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class AuthStoreError(RuntimeError):
    """Base class for sanitized authentication-state failures."""

    def __init__(self, state_id: str, message: str) -> None:
        self.state_id = state_id
        super().__init__(message)


class AuthStateNotFoundError(AuthStoreError):
    def __init__(self, state_id: str) -> None:
        super().__init__(state_id, "Authentication state was not found")


class AuthStateExpiredError(AuthStoreError):
    def __init__(self, state_id: str) -> None:
        super().__init__(state_id, "Authentication state has expired")


class AuthStateAlreadyUsedError(AuthStoreError):
    def __init__(self, state_id: str) -> None:
        super().__init__(state_id, "Authentication state was already used")


class AuthStateAlreadyExistsError(AuthStoreError):
    def __init__(self, state_id: str) -> None:
        super().__init__(state_id, "Authentication state already exists")


class AuthStateInvalidError(AuthStoreError):
    def __init__(self, state_id: str) -> None:
        super().__init__(state_id, "Stored authentication state is invalid")


class AuthSessionRevokedError(AuthStoreError):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id, "Authentication session was revoked")


class _AuthChallengeState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["1"] = AUTH_STATE_VERSION
    kind: AuthChallengeKind
    challenge_id: Annotated[str, Field(pattern=_CHALLENGE_ID_PATTERN)]
    challenge: Annotated[str, Field(min_length=43, max_length=128, pattern=_BASE64URL_PATTERN)]
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
            raise ValueError("Authentication timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_lifetime(self) -> _AuthChallengeState:
        if self.expires_at <= self.issued_at:
            raise ValueError("Authentication challenge expiry must follow issuance")
        return self


class SignupChallengeState(_AuthChallengeState):
    """Redis-only state for first-passkey account creation."""

    kind: Literal["signup"] = "signup"
    account_id: Annotated[str, Field(pattern=_ACCOUNT_ID_PATTERN)]
    approval_identity_id: Annotated[str, Field(pattern=_APPROVAL_IDENTITY_ID_PATTERN)]
    user_handle: Annotated[str, Field(min_length=43, max_length=86, pattern=_BASE64URL_PATTERN)]
    display_name: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("user_handle", mode="after")
    @classmethod
    def validate_user_handle(cls, value: str) -> str:
        try:
            decoded = decode_base64url(value, maximum_bytes=64)
        except ValueError as error:
            raise ValueError("User handle must be unpadded base64url") from error
        if not 1 <= len(decoded) <= 64:
            raise ValueError("User handle must contain between one and 64 bytes")
        return value

    @field_validator("display_name", mode="after")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Display name must be normalized")
        return value


class LoginChallengeState(_AuthChallengeState):
    """Identity-free Redis state for discoverable-credential login."""

    kind: Literal["login"] = "login"


class AuthSessionState(BaseModel):
    """Strict server-side session state; the browser receives only its opaque ID."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["1"] = AUTH_STATE_VERSION
    session_id: Annotated[str, Field(pattern=_SESSION_ID_PATTERN)]
    account_id: Annotated[str, Field(pattern=_ACCOUNT_ID_PATTERN)]
    approval_identity_id: Annotated[str, Field(pattern=_APPROVAL_IDENTITY_ID_PATTERN)]
    passkey_credential_id: Annotated[str, Field(pattern=_PASSKEY_CREDENTIAL_ID_PATTERN)]
    account_session_version: Annotated[int, Field(ge=1, le=2_147_483_647)]
    csrf_token: Annotated[str, Field(pattern=_OPAQUE_SECRET_PATTERN)]
    created_at: datetime
    authenticated_at: datetime
    expires_at: datetime
    auth_method: Literal["passkey"] = "passkey"

    @field_validator("created_at", "authenticated_at", "expires_at", mode="after")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Session timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_lifetime(self) -> AuthSessionState:
        if self.authenticated_at < self.created_at or self.expires_at <= self.authenticated_at:
            raise ValueError("Session timestamps are inconsistent")
        return self


@runtime_checkable
class AuthStore(Protocol):
    async def save_signup(self, state: SignupChallengeState, *, ttl_seconds: int) -> None: ...

    async def consume_signup(self, challenge_id: str) -> SignupChallengeState: ...

    async def save_login(self, state: LoginChallengeState, *, ttl_seconds: int) -> None: ...

    async def consume_login(self, challenge_id: str) -> LoginChallengeState: ...

    async def save_session(self, state: AuthSessionState, *, ttl_seconds: int) -> None: ...

    async def get_session(self, session_id: str) -> AuthSessionState: ...

    async def revoke_session(self, session_id: str) -> None: ...


@runtime_checkable
class RedisAuthClient(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> Any: ...


_CREATE_SCRIPT = """
local state_key = KEYS[1]
local status_key = KEYS[2]
if redis.call('EXISTS', state_key) == 1 or redis.call('EXISTS', status_key) == 1 then
  return 0
end
redis.call('SET', state_key, ARGV[1], 'EX', ARGV[2])
redis.call('SET', status_key, ARGV[3], 'EX', ARGV[4])
return 1
"""

_CONSUME_CHALLENGE_SCRIPT = """
local state_key = KEYS[1]
local status_key = KEYS[2]
local payload = redis.call('GETDEL', state_key)
if payload then
  redis.call('SET', status_key, 'consumed', 'EX', ARGV[1])
  return {1, payload}
end
local status = redis.call('GET', status_key)
if status == 'consumed' then return {2, false} end
if status == 'issued' then return {3, false} end
return {0, false}
"""

_GET_SESSION_SCRIPT = """
local payload = redis.call('GET', KEYS[1])
if payload then return {1, payload} end
local status = redis.call('GET', KEYS[2])
if status == 'revoked' then return {2, false} end
if status == 'active' then return {3, false} end
return {0, false}
"""

_REVOKE_SESSION_SCRIPT = """
local payload = redis.call('GETDEL', KEYS[1])
if payload then
  redis.call('SET', KEYS[2], 'revoked', 'EX', ARGV[1])
  return 1
end
local status = redis.call('GET', KEYS[2])
if status == 'revoked' then return 2 end
if status == 'active' then return 3 end
return 0
"""

ChallengeStateT = TypeVar("ChallengeStateT", SignupChallengeState, LoginChallengeState)
StoredStateT = TypeVar(
    "StoredStateT",
    SignupChallengeState,
    LoginChallengeState,
    AuthSessionState,
)


class RedisAuthStore:
    """Redis implementation with atomic challenge use and server-side revocation."""

    def __init__(
        self,
        client: RedisAuthClient,
        *,
        namespace: str = "metergate:v1:auth",
        clock: Clock = utc_now,
    ) -> None:
        if _NAMESPACE_PATTERN.fullmatch(namespace) is None:
            raise ValueError("Redis authentication namespace contains unsupported characters")
        self._client = client
        self._namespace = namespace.rstrip(":")
        self._clock = clock

    async def save_signup(self, state: SignupChallengeState, *, ttl_seconds: int) -> None:
        await self._save_challenge("signup", state, ttl_seconds=ttl_seconds)

    async def consume_signup(self, challenge_id: str) -> SignupChallengeState:
        return await self._consume_challenge("signup", challenge_id, SignupChallengeState)

    async def save_login(self, state: LoginChallengeState, *, ttl_seconds: int) -> None:
        await self._save_challenge("login", state, ttl_seconds=ttl_seconds)

    async def consume_login(self, challenge_id: str) -> LoginChallengeState:
        return await self._consume_challenge("login", challenge_id, LoginChallengeState)

    async def save_session(self, state: AuthSessionState, *, ttl_seconds: int) -> None:
        self._validate_ttl(ttl_seconds, maximum=86_400)
        payload = self._serialize(state, state.session_id)
        state_key, status_key = self._session_keys(state.session_id)
        created = await self._client.eval(
            _CREATE_SCRIPT,
            2,
            state_key,
            status_key,
            payload,
            ttl_seconds,
            "active",
            ttl_seconds * 2,
        )
        if created != 1:
            raise AuthStateAlreadyExistsError(state.session_id)

    async def get_session(self, session_id: str) -> AuthSessionState:
        self._validate_session_id(session_id)
        state_key, status_key = self._session_keys(session_id)
        result = await self._client.eval(_GET_SESSION_SCRIPT, 2, state_key, status_key)
        outcome, payload = self._result(result, session_id)
        if outcome == 0:
            raise AuthStateNotFoundError(session_id)
        if outcome == 2:
            raise AuthSessionRevokedError(session_id)
        if outcome == 3:
            raise AuthStateExpiredError(session_id)
        if outcome != 1 or not isinstance(payload, (bytes, str)):
            raise AuthStateInvalidError(session_id)
        state = self._parse(payload, session_id, AuthSessionState)
        if state.session_id != session_id:
            raise AuthStateInvalidError(session_id)
        if self._read_clock() >= state.expires_at:
            raise AuthStateExpiredError(session_id)
        return state

    async def revoke_session(self, session_id: str) -> None:
        self._validate_session_id(session_id)
        state_key, status_key = self._session_keys(session_id)
        outcome = await self._client.eval(
            _REVOKE_SESSION_SCRIPT,
            2,
            state_key,
            status_key,
            _CONSUMED_MARKER_TTL_SECONDS,
        )
        if outcome == 0:
            raise AuthStateNotFoundError(session_id)
        if outcome == 2:
            raise AuthSessionRevokedError(session_id)
        if outcome == 3:
            raise AuthStateExpiredError(session_id)
        if outcome != 1:
            raise AuthStateInvalidError(session_id)

    async def _save_challenge(
        self,
        kind: AuthChallengeKind,
        state: _AuthChallengeState,
        *,
        ttl_seconds: int,
    ) -> None:
        if state.kind != kind:
            raise AuthStateInvalidError(state.challenge_id)
        self._validate_ttl(ttl_seconds, maximum=600)
        payload = self._serialize(state, state.challenge_id)
        state_key, status_key = self._challenge_keys(kind, state.challenge_id)
        created = await self._client.eval(
            _CREATE_SCRIPT,
            2,
            state_key,
            status_key,
            payload,
            ttl_seconds,
            "issued",
            ttl_seconds * 2,
        )
        if created != 1:
            raise AuthStateAlreadyExistsError(state.challenge_id)

    async def _consume_challenge(
        self,
        kind: AuthChallengeKind,
        challenge_id: str,
        state_type: type[ChallengeStateT],
    ) -> ChallengeStateT:
        self._validate_challenge_id(challenge_id)
        state_key, status_key = self._challenge_keys(kind, challenge_id)
        result = await self._client.eval(
            _CONSUME_CHALLENGE_SCRIPT,
            2,
            state_key,
            status_key,
            _CONSUMED_MARKER_TTL_SECONDS,
        )
        outcome, payload = self._result(result, challenge_id)
        if outcome == 0:
            raise AuthStateNotFoundError(challenge_id)
        if outcome == 2:
            raise AuthStateAlreadyUsedError(challenge_id)
        if outcome == 3:
            raise AuthStateExpiredError(challenge_id)
        if outcome != 1 or not isinstance(payload, (bytes, str)):
            raise AuthStateInvalidError(challenge_id)
        state = self._parse(payload, challenge_id, state_type)
        if state.challenge_id != challenge_id or state.kind != kind:
            raise AuthStateInvalidError(challenge_id)
        if self._read_clock() >= state.expires_at:
            raise AuthStateExpiredError(challenge_id)
        return state

    def _challenge_keys(self, kind: AuthChallengeKind, challenge_id: str) -> tuple[str, str]:
        base = f"{self._namespace}:{kind}:{{{challenge_id}}}"
        return f"{base}:state", f"{base}:status"

    def _session_keys(self, session_id: str) -> tuple[str, str]:
        base = f"{self._namespace}:session:{{{session_id}}}"
        return f"{base}:state", f"{base}:status"

    @staticmethod
    def _serialize(state: BaseModel, state_id: str) -> bytes:
        try:
            payload = state.model_dump(mode="json")
            for field in ("issued_at", "created_at", "authenticated_at", "expires_at"):
                if field in payload:
                    payload[field] = canonical_utc_datetime(getattr(state, field))
            return canonical_json_bytes(payload)
        except (CanonicalJSONError, TypeError, ValueError) as error:
            raise AuthStateInvalidError(state_id) from error

    @staticmethod
    def _parse(payload: bytes | str, state_id: str, state_type: type[StoredStateT]) -> StoredStateT:
        try:
            return state_type.model_validate_json(payload)
        except (ValidationError, TypeError, ValueError) as error:
            raise AuthStateInvalidError(state_id) from error

    @staticmethod
    def _result(result: Any, state_id: str) -> tuple[Any, Any]:
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise AuthStateInvalidError(state_id)
        return result[0], result[1]

    @staticmethod
    def _validate_ttl(ttl_seconds: int, *, maximum: int) -> None:
        if isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= maximum:
            raise ValueError(f"Authentication TTL must be between one and {maximum} seconds")

    @staticmethod
    def _validate_challenge_id(challenge_id: str) -> None:
        if re.fullmatch(_CHALLENGE_ID_PATTERN, challenge_id) is None:
            raise AuthStateNotFoundError(challenge_id)

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if re.fullmatch(_SESSION_ID_PATTERN, session_id) is None:
            raise AuthStateNotFoundError(session_id)

    def _read_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Authentication store clock must be timezone-aware")
        return value.astimezone(UTC)
