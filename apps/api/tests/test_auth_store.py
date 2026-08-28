from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.cache.auth import (
    AuthSessionRevokedError,
    AuthSessionState,
    AuthStateAlreadyExistsError,
    AuthStateAlreadyUsedError,
    AuthStateExpiredError,
    AuthStateInvalidError,
    AuthStateNotFoundError,
    LoginChallengeState,
    RedisAuthStore,
    SignupChallengeState,
)
from app.domain.base64url import encode_base64url

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
CHALLENGE_ID = "ach_00000000000000000000000001"
ACCOUNT_ID = "acct_00000000000000000000000001"
IDENTITY_ID = "aid_00000000000000000000000001"
CREDENTIAL_ID = "pkc_00000000000000000000000001"
SESSION_ID = f"ses_{encode_base64url(b's' * 32)}"
CSRF_TOKEN = encode_base64url(b"c" * 32)


@dataclass
class FrozenClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


class AtomicFakeRedis:
    """Script-aware Redis double for the store's atomic protocol."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.ttls: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        assert numkeys == 2
        state_key, status_key = str(keys_and_args[0]), str(keys_and_args[1])
        async with self._lock:
            if "EXISTS" in script:
                if state_key in self.values or status_key in self.values:
                    return 0
                self.values[state_key] = keys_and_args[2]
                self.ttls[state_key] = int(keys_and_args[3])
                self.values[status_key] = keys_and_args[4]
                self.ttls[status_key] = int(keys_and_args[5])
                return 1
            if "GETDEL" in script and "return {1, payload}" in script:
                payload = self.values.pop(state_key, None)
                if payload is not None:
                    self.values[status_key] = "consumed"
                    return [1, payload]
                status = self.values.get(status_key)
                if status == "consumed":
                    return [2, False]
                if status == "issued":
                    return [3, False]
                return [0, False]
            if "GETDEL" not in script and "local payload = redis.call('GET'" in script:
                payload = self.values.get(state_key)
                if payload is not None:
                    return [1, payload]
                status = self.values.get(status_key)
                if status == "revoked":
                    return [2, False]
                if status == "active":
                    return [3, False]
                return [0, False]

            payload = self.values.pop(state_key, None)
            if payload is not None:
                self.values[status_key] = "revoked"
                return 1
            status = self.values.get(status_key)
            if status == "revoked":
                return 2
            if status == "active":
                return 3
            return 0


def signup_state(
    *,
    challenge_id: str = CHALLENGE_ID,
    expires_at: datetime = NOW + timedelta(minutes=5),
) -> SignupChallengeState:
    return SignupChallengeState(
        challenge_id=challenge_id,
        challenge=encode_base64url(b"q" * 32),
        account_id=ACCOUNT_ID,
        approval_identity_id=IDENTITY_ID,
        user_handle=encode_base64url(b"u" * 32),
        display_name="Test Buyer",
        issued_at=NOW,
        expires_at=expires_at,
    )


def login_state(
    *,
    challenge_id: str = CHALLENGE_ID,
    expires_at: datetime = NOW + timedelta(minutes=5),
) -> LoginChallengeState:
    return LoginChallengeState(
        challenge_id=challenge_id,
        challenge=encode_base64url(b"l" * 32),
        issued_at=NOW,
        expires_at=expires_at,
    )


def session_state(*, expires_at: datetime = NOW + timedelta(hours=1)) -> AuthSessionState:
    return AuthSessionState(
        session_id=SESSION_ID,
        account_id=ACCOUNT_ID,
        approval_identity_id=IDENTITY_ID,
        passkey_credential_id=CREDENTIAL_ID,
        account_session_version=1,
        csrf_token=CSRF_TOKEN,
        created_at=NOW,
        authenticated_at=NOW,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["signup", "login"])
async def test_auth_challenges_are_atomic_single_use(kind: str) -> None:
    redis = AtomicFakeRedis()
    store = RedisAuthStore(redis, clock=FrozenClock(NOW))
    if kind == "signup":
        await store.save_signup(signup_state(), ttl_seconds=300)
        consume = store.consume_signup
        expected_type = SignupChallengeState
    else:
        await store.save_login(login_state(), ttl_seconds=300)
        consume = store.consume_login
        expected_type = LoginChallengeState

    results = await asyncio.gather(
        consume(CHALLENGE_ID),
        consume(CHALLENGE_ID),
        return_exceptions=True,
    )

    assert sum(isinstance(result, expected_type) for result in results) == 1
    assert sum(isinstance(result, AuthStateAlreadyUsedError) for result in results) == 1


@pytest.mark.asyncio
async def test_auth_challenge_namespaces_do_not_cross_consume() -> None:
    redis = AtomicFakeRedis()
    store = RedisAuthStore(redis, clock=FrozenClock(NOW))
    await store.save_signup(signup_state(), ttl_seconds=300)

    with pytest.raises(AuthStateNotFoundError):
        await store.consume_login(CHALLENGE_ID)
    assert (await store.consume_signup(CHALLENGE_ID)).kind == "signup"


@pytest.mark.asyncio
async def test_auth_challenge_duplicate_expired_and_malformed_states_fail_closed() -> None:
    redis = AtomicFakeRedis()
    clock = FrozenClock(NOW)
    store = RedisAuthStore(redis, clock=clock)
    await store.save_signup(signup_state(), ttl_seconds=300)
    state_key, _ = store._challenge_keys("signup", CHALLENGE_ID)  # noqa: SLF001

    with pytest.raises(AuthStateAlreadyExistsError):
        await store.save_signup(signup_state(), ttl_seconds=300)

    redis.values[state_key] = b"{}"
    with pytest.raises(AuthStateInvalidError):
        await store.consume_signup(CHALLENGE_ID)

    second_id = "ach_00000000000000000000000002"
    await store.save_signup(signup_state(challenge_id=second_id), ttl_seconds=300)
    clock.current = NOW + timedelta(minutes=5)
    with pytest.raises(AuthStateExpiredError):
        await store.consume_signup(second_id)


@pytest.mark.asyncio
async def test_session_is_strict_redis_state_with_absolute_expiry_and_revocation() -> None:
    redis = AtomicFakeRedis()
    clock = FrozenClock(NOW)
    store = RedisAuthStore(redis, clock=clock)
    state = session_state()

    await store.save_session(state, ttl_seconds=3600)
    assert redis.ttls[store._session_keys(SESSION_ID)[0]] == 3600  # noqa: SLF001
    assert await store.get_session(SESSION_ID) == state

    await store.revoke_session(SESSION_ID)
    with pytest.raises(AuthSessionRevokedError):
        await store.get_session(SESSION_ID)
    with pytest.raises(AuthSessionRevokedError):
        await store.revoke_session(SESSION_ID)


@pytest.mark.asyncio
async def test_session_elapsed_or_missing_state_fails_closed() -> None:
    redis = AtomicFakeRedis()
    clock = FrozenClock(NOW)
    store = RedisAuthStore(redis, clock=clock)
    await store.save_session(session_state(), ttl_seconds=3600)
    clock.current = NOW + timedelta(hours=1)

    with pytest.raises(AuthStateExpiredError):
        await store.get_session(SESSION_ID)
    with pytest.raises(AuthStateNotFoundError):
        await store.get_session(f"ses_{encode_base64url(b'x' * 32)}")
    with pytest.raises(AuthStateNotFoundError):
        await store.get_session("not-a-session")


def test_auth_states_reject_weak_secrets_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SignupChallengeState.model_validate(
            {
                **signup_state().model_dump(),
                "challenge": encode_base64url(b"short"),
            }
        )
    with pytest.raises(ValidationError):
        AuthSessionState.model_validate(
            {
                **session_state().model_dump(),
                "csrf_token": "short",
                "unexpected": True,
            }
        )


@pytest.mark.asyncio
async def test_real_redis_auth_ttl_replay_and_revocation_when_configured() -> None:
    redis_url = os.getenv("METERGATE_TEST_REDIS_URL")
    if redis_url is None:
        pytest.skip("Set METERGATE_TEST_REDIS_URL to run the real Redis auth test")

    client = Redis.from_url(redis_url, decode_responses=False)
    try:
        await client.ping()
    except RedisError as error:
        await client.aclose()
        pytest.skip(f"Configured test Redis is unavailable: {error.__class__.__name__}")

    namespace = f"metergate:test:auth:{uuid4().hex}"
    store = RedisAuthStore(client, namespace=namespace)
    now = datetime.now(UTC)
    challenge_id = "ach_00000000000000000000000003"
    challenge = login_state(
        challenge_id=challenge_id,
        expires_at=now + timedelta(seconds=10),
    ).model_copy(update={"issued_at": now})
    live_session = session_state(expires_at=now + timedelta(seconds=10)).model_copy(
        update={"created_at": now, "authenticated_at": now}
    )
    try:
        await store.save_login(challenge, ttl_seconds=10)
        results = await asyncio.gather(
            store.consume_login(challenge_id),
            store.consume_login(challenge_id),
            return_exceptions=True,
        )
        assert sum(isinstance(result, LoginChallengeState) for result in results) == 1
        assert sum(isinstance(result, AuthStateAlreadyUsedError) for result in results) == 1

        await store.save_session(live_session, ttl_seconds=10)
        assert (await store.get_session(SESSION_ID)).account_id == ACCOUNT_ID
        await store.revoke_session(SESSION_ID)
        with pytest.raises(AuthSessionRevokedError):
            await store.get_session(SESSION_ID)
    finally:
        keys = [key async for key in client.scan_iter(match=f"{namespace}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()
