import asyncio

import pytest

from app.errors import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyIntegrityError,
    IdempotencyUncertainError,
)
from app.idempotency import (
    ExecutionClaim,
    RedisExecutionIdempotency,
    ReplayedExecution,
)
from tests.conftest import FULFILLMENT_ID
from tests.fakes import FakeRedis

REQUEST_HASH = "sha256:" + "a" * 64
OTHER_REQUEST_HASH = "sha256:" + "b" * 64


def make_store(redis: FakeRedis, **changes: object) -> RedisExecutionIdempotency:
    values: dict[str, object] = {
        "ttl_seconds": 3_600,
        "lock_ttl_seconds": 60,
        "wait_seconds": 0.1,
        "poll_seconds": 0.001,
        "maximum_result_bytes": 4_096,
    }
    values.update(changes)
    return RedisExecutionIdempotency(redis, **values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_success_is_replayed_byte_for_byte(fake_redis: FakeRedis) -> None:
    store = make_store(fake_redis)
    claimed = await store.claim(FULFILLMENT_ID, REQUEST_HASH)
    assert isinstance(claimed, ExecutionClaim)
    body = b'{"exact":"stored-result"}'
    await store.complete(claimed, body)

    replayed = await store.claim(FULFILLMENT_ID, REQUEST_HASH)

    assert isinstance(replayed, ReplayedExecution)
    assert replayed.response_body == body


@pytest.mark.asyncio
async def test_execution_id_cannot_be_rebound(fake_redis: FakeRedis) -> None:
    store = make_store(fake_redis)
    assert isinstance(await store.claim(FULFILLMENT_ID, REQUEST_HASH), ExecutionClaim)

    with pytest.raises(IdempotencyConflictError) as captured:
        await store.claim(FULFILLMENT_ID, OTHER_REQUEST_HASH)

    assert captured.value.reason_code == "ORBITINTEL_IDEMPOTENCY_MISMATCH"


@pytest.mark.asyncio
async def test_result_without_request_binding_cannot_be_adopted(
    fake_redis: FakeRedis,
) -> None:
    fake_redis.values[f"orbitintel:idempotency:result:v1:{FULFILLMENT_ID}"] = b'{"old":true}'
    store = make_store(fake_redis)

    with pytest.raises(IdempotencyIntegrityError) as captured:
        await store.claim(FULFILLMENT_ID, REQUEST_HASH)

    assert captured.value.reason_code == "ORBITINTEL_IDEMPOTENCY_INTEGRITY_FAILED"
    assert f"orbitintel:idempotency:binding:v1:{FULFILLMENT_ID}" not in fake_redis.values


@pytest.mark.asyncio
async def test_active_duplicate_waits_for_first_result(fake_redis: FakeRedis) -> None:
    store = make_store(fake_redis)
    first = await store.claim(FULFILLMENT_ID, REQUEST_HASH)
    assert isinstance(first, ExecutionClaim)
    duplicate = asyncio.create_task(store.claim(FULFILLMENT_ID, REQUEST_HASH))
    await asyncio.sleep(0.005)
    await store.complete(first, b'{"ok":true}')

    result = await duplicate

    assert isinstance(result, ReplayedExecution)
    assert result.response_body == b'{"ok":true}'


@pytest.mark.asyncio
async def test_active_duplicate_times_out_with_stable_code(fake_redis: FakeRedis) -> None:
    store = make_store(fake_redis, wait_seconds=0.002, poll_seconds=0.001)
    assert isinstance(await store.claim(FULFILLMENT_ID, REQUEST_HASH), ExecutionClaim)

    with pytest.raises(IdempotencyInProgressError) as captured:
        await store.claim(FULFILLMENT_ID, REQUEST_HASH)

    assert captured.value.reason_code == "ORBITINTEL_EXECUTION_IN_PROGRESS"


@pytest.mark.asyncio
async def test_expired_lock_with_started_marker_fails_closed_as_uncertain(
    fake_redis: FakeRedis,
) -> None:
    store = make_store(fake_redis)
    claim = await store.claim(FULFILLMENT_ID, REQUEST_HASH)
    assert isinstance(claim, ExecutionClaim)
    del fake_redis.values[f"orbitintel:idempotency:lock:v1:{FULFILLMENT_ID}"]

    with pytest.raises(IdempotencyUncertainError) as captured:
        await store.claim(FULFILLMENT_ID, REQUEST_HASH)

    assert captured.value.reason_code == "ORBITINTEL_EXECUTION_UNCERTAIN"


@pytest.mark.asyncio
async def test_retry_safe_abandon_clears_started_marker_and_allows_retry(
    fake_redis: FakeRedis,
) -> None:
    store = make_store(fake_redis)
    first = await store.claim(FULFILLMENT_ID, REQUEST_HASH)
    assert isinstance(first, ExecutionClaim)

    await store.abandon(first, retry_safe=True)
    second = await store.claim(FULFILLMENT_ID, REQUEST_HASH)

    assert isinstance(second, ExecutionClaim)
    assert second.token != first.token


@pytest.mark.asyncio
async def test_lost_lease_cannot_persist_result(fake_redis: FakeRedis) -> None:
    store = make_store(fake_redis)
    claim = await store.claim(FULFILLMENT_ID, REQUEST_HASH)
    assert isinstance(claim, ExecutionClaim)
    fake_redis.values[f"orbitintel:idempotency:lock:v1:{FULFILLMENT_ID}"] = b"different-owner"

    with pytest.raises(IdempotencyIntegrityError) as captured:
        await store.complete(claim, b'{"ok":true}')

    assert captured.value.reason_code == "ORBITINTEL_EXECUTION_LEASE_LOST"


@pytest.mark.asyncio
async def test_changed_request_binding_cannot_persist_result(fake_redis: FakeRedis) -> None:
    store = make_store(fake_redis)
    claim = await store.claim(FULFILLMENT_ID, REQUEST_HASH)
    assert isinstance(claim, ExecutionClaim)
    fake_redis.values[f"orbitintel:idempotency:binding:v1:{FULFILLMENT_ID}"] = (
        OTHER_REQUEST_HASH.encode()
    )

    with pytest.raises(IdempotencyIntegrityError) as captured:
        await store.complete(claim, b'{"ok":true}')

    assert captured.value.reason_code == "ORBITINTEL_IDEMPOTENCY_INTEGRITY_FAILED"
    assert f"orbitintel:idempotency:result:v1:{FULFILLMENT_ID}" not in fake_redis.values


@pytest.mark.asyncio
async def test_result_size_is_bounded(fake_redis: FakeRedis) -> None:
    store = make_store(fake_redis, maximum_result_bytes=4)
    claim = await store.claim(FULFILLMENT_ID, REQUEST_HASH)
    assert isinstance(claim, ExecutionClaim)

    with pytest.raises(IdempotencyIntegrityError) as captured:
        await store.complete(claim, b"12345")

    assert captured.value.reason_code == "ORBITINTEL_RESULT_SIZE_INVALID"
