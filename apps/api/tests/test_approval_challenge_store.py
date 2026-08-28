from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.cache.approval_challenges import (
    ApprovalChallengeState,
    ChallengeAlreadyExistsError,
    ChallengeAlreadyUsedError,
    ChallengeExpiredError,
    ChallengeNotFoundError,
    RedisChallengeStore,
    RegistrationChallengeState,
)
from app.domain.base64url import encode_base64url
from app.domain.hashing import sha256_bytes

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
CHALLENGE_ID = "ach_00000000000000000000000001"
IDENTITY_ID = "aid_00000000000000000000000001"
ACCOUNT_ID = "acct_00000000000000000000000001"
OTHER_ACCOUNT_ID = "acct_00000000000000000000000002"
SESSION_ID = "ses_00000000000000000000000001"


@dataclass
class FrozenClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


class AtomicFakeRedis:
    """Small script-aware Redis double; application code still uses the real Lua boundary."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
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
                self.values[status_key] = "issued"
                return 1
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


def registration_state(
    *,
    challenge_id: str = CHALLENGE_ID,
    expires_at: datetime = NOW + timedelta(minutes=5),
) -> RegistrationChallengeState:
    return RegistrationChallengeState(
        challenge_id=challenge_id,
        challenge=encode_base64url(b"r" * 32),
        account_id=ACCOUNT_ID,
        approval_identity_id=IDENTITY_ID,
        session_id_hash=sha256_bytes(SESSION_ID.encode()),
        user_handle=encode_base64url(b"u" * 32),
        issued_at=NOW,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_challenge_store_consumes_exactly_once_under_concurrency() -> None:
    redis = AtomicFakeRedis()
    store = RedisChallengeStore(redis, clock=FrozenClock(NOW))
    await store.save_registration(registration_state(), ttl_seconds=300)

    results = await asyncio.gather(
        store.consume_registration(CHALLENGE_ID, account_id=ACCOUNT_ID),
        store.consume_registration(CHALLENGE_ID, account_id=ACCOUNT_ID),
        return_exceptions=True,
    )

    assert sum(isinstance(result, RegistrationChallengeState) for result in results) == 1
    assert sum(isinstance(result, ChallengeAlreadyUsedError) for result in results) == 1


@pytest.mark.asyncio
async def test_challenge_store_rejects_duplicate_id_and_distinguishes_states() -> None:
    redis = AtomicFakeRedis()
    store = RedisChallengeStore(redis, clock=FrozenClock(NOW))
    await store.save_registration(registration_state(), ttl_seconds=300)

    with pytest.raises(ChallengeAlreadyExistsError):
        await store.save_registration(registration_state(), ttl_seconds=300)
    with pytest.raises(ChallengeNotFoundError):
        await store.consume_registration(
            "ach_00000000000000000000000002",
            account_id=ACCOUNT_ID,
        )

    state_key, _ = store._keys(  # noqa: SLF001
        "registration",
        ACCOUNT_ID,
        CHALLENGE_ID,
    )
    redis.values.pop(state_key)
    with pytest.raises(ChallengeExpiredError):
        await store.consume_registration(CHALLENGE_ID, account_id=ACCOUNT_ID)


@pytest.mark.asyncio
async def test_challenge_store_rejects_elapsed_payload_even_if_redis_returns_it() -> None:
    clock = FrozenClock(NOW)
    store = RedisChallengeStore(AtomicFakeRedis(), clock=clock)
    await store.save_registration(registration_state(), ttl_seconds=300)
    clock.current = NOW + timedelta(minutes=5)

    with pytest.raises(ChallengeExpiredError):
        await store.consume_registration(CHALLENGE_ID, account_id=ACCOUNT_ID)


@pytest.mark.asyncio
async def test_registration_and_approval_namespaces_do_not_cross_consume() -> None:
    redis = AtomicFakeRedis()
    store = RedisChallengeStore(redis, clock=FrozenClock(NOW))
    await store.save_registration(registration_state(), ttl_seconds=300)

    with pytest.raises(ChallengeNotFoundError):
        await store.consume_approval(CHALLENGE_ID, account_id=ACCOUNT_ID)
    assert (
        await store.consume_registration(CHALLENGE_ID, account_id=ACCOUNT_ID)
    ).kind == "registration"


@pytest.mark.asyncio
async def test_other_account_cannot_consume_or_burn_a_challenge() -> None:
    store = RedisChallengeStore(AtomicFakeRedis(), clock=FrozenClock(NOW))
    await store.save_registration(registration_state(), ttl_seconds=300)

    with pytest.raises(ChallengeNotFoundError):
        await store.consume_registration(CHALLENGE_ID, account_id=OTHER_ACCOUNT_ID)

    state = await store.consume_registration(CHALLENGE_ID, account_id=ACCOUNT_ID)
    assert state.account_id == ACCOUNT_ID


@pytest.mark.asyncio
async def test_real_redis_challenge_ttl_and_atomic_replay_when_configured() -> None:
    redis_url = os.getenv("METERGATE_TEST_REDIS_URL")
    if redis_url is None:
        pytest.skip("Set METERGATE_TEST_REDIS_URL to run the real Redis approval test")

    client = Redis.from_url(redis_url, decode_responses=False)
    try:
        await client.ping()
    except RedisError as error:
        await client.aclose()
        pytest.skip(f"Configured test Redis is unavailable: {error.__class__.__name__}")

    namespace = f"metergate:test:approval:{uuid4().hex}"
    store = RedisChallengeStore(client, namespace=namespace)
    challenge_id = "ach_00000000000000000000000003"
    now = datetime.now(UTC)
    state = registration_state(
        challenge_id=challenge_id,
        expires_at=now + timedelta(seconds=2),
    ).model_copy(update={"issued_at": now})
    try:
        await store.save_registration(state, ttl_seconds=2)
        results = await asyncio.gather(
            store.consume_registration(challenge_id, account_id=ACCOUNT_ID),
            store.consume_registration(challenge_id, account_id=ACCOUNT_ID),
            return_exceptions=True,
        )
        assert sum(isinstance(result, RegistrationChallengeState) for result in results) == 1
        assert sum(isinstance(result, ChallengeAlreadyUsedError) for result in results) == 1

        state_key, status_key = store._keys(  # noqa: SLF001
            "registration",
            ACCOUNT_ID,
            challenge_id,
        )
        assert await client.exists(state_key) == 0
        assert await client.ttl(status_key) > 0

        expiring_id = "ach_00000000000000000000000004"
        expiring_now = datetime.now(UTC)
        expiring = registration_state(
            challenge_id=expiring_id,
            expires_at=expiring_now + timedelta(seconds=1),
        ).model_copy(update={"issued_at": expiring_now})
        await store.save_registration(expiring, ttl_seconds=1)
        await asyncio.sleep(1.1)
        with pytest.raises(ChallengeExpiredError):
            await store.consume_registration(expiring_id, account_id=ACCOUNT_ID)
    finally:
        keys = [key async for key in client.scan_iter(match=f"{namespace}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def test_approval_state_forbids_duplicate_credential_bindings() -> None:
    from pydantic import ValidationError

    from app.cache.approval_challenges import AllowedCredentialBinding

    binding = AllowedCredentialBinding(
        passkey_credential_id="pkc_00000000000000000000000001",
        credential_id_hash=f"sha256:{'1' * 64}",
    )
    with pytest.raises(ValidationError, match="must be unique"):
        ApprovalChallengeState(
            challenge_id=CHALLENGE_ID,
            challenge=encode_base64url(b"a" * 32),
            account_id=ACCOUNT_ID,
            approval_identity_id=IDENTITY_ID,
            evaluation_id="pye_00000000000000000000000001",
            policy_id="pol_00000000000000000000000001",
            policy_hash=f"sha256:{'2' * 64}",
            quote_id="qte_00000000000000000000000001",
            quote_hash=f"sha256:{'3' * 64}",
            review={"review_version": "1"},
            review_hash=f"sha256:{'4' * 64}",
            amount=500,
            currency="INR",
            allowed_credentials=[binding, binding],
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
