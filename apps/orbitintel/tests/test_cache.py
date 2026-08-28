import asyncio

import pytest

from app.cache import RedisGPSnapshotCache
from app.errors import CacheUnavailableError
from app.schemas import GPSnapshot
from tests.fakes import FakeRedis


def make_cache(redis: FakeRedis, **changes: object) -> RedisGPSnapshotCache:
    values: dict[str, object] = {
        "ttl_seconds": 180,
        "lock_ttl_ms": 30_000,
        "wait_seconds": 0.2,
        "poll_seconds": 0.001,
    }
    values.update(changes)
    return RedisGPSnapshotCache(redis, **values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cache_hit_skips_loader(
    fake_redis: FakeRedis,
    snapshot: GPSnapshot,
) -> None:
    cache = make_cache(fake_redis)
    await fake_redis.set(
        "orbitintel:celestrak:gp:v1:25544",
        snapshot.model_dump_json().encode(),
        ex=180,
    )
    called = False

    async def loader() -> GPSnapshot:
        nonlocal called
        called = True
        return snapshot

    loaded = await cache.get_or_load(25544, loader)

    assert loaded == snapshot
    assert called is False


@pytest.mark.asyncio
async def test_concurrent_miss_runs_one_loader(
    fake_redis: FakeRedis,
    snapshot: GPSnapshot,
) -> None:
    cache = make_cache(fake_redis)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def loader() -> GPSnapshot:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return snapshot

    first = asyncio.create_task(cache.get_or_load(25544, loader))
    await started.wait()
    second = asyncio.create_task(cache.get_or_load(25544, loader))
    await asyncio.sleep(0)
    release.set()

    assert await first == snapshot
    assert await second == snapshot
    assert calls == 1


@pytest.mark.asyncio
async def test_corrupt_cache_is_removed_and_reloaded(
    fake_redis: FakeRedis,
    snapshot: GPSnapshot,
) -> None:
    cache = make_cache(fake_redis)
    key = "orbitintel:celestrak:gp:v1:25544"
    await fake_redis.set(key, b'{"norad_id":true}', ex=180)

    loaded = await cache.get_or_load(25544, lambda: _return(snapshot))

    assert loaded == snapshot
    assert b'"object_name":"ISS (ZARYA)"' in fake_redis.values[key]


@pytest.mark.asyncio
async def test_loader_failure_is_not_cached_and_lock_is_released(
    fake_redis: FakeRedis,
) -> None:
    cache = make_cache(fake_redis)

    async def loader() -> GPSnapshot:
        raise RuntimeError("upstream failed")

    with pytest.raises(RuntimeError, match="upstream failed"):
        await cache.get_or_load(25544, loader)

    assert "orbitintel:celestrak:gp:v1:25544" not in fake_redis.values
    assert "orbitintel:celestrak:gp-lock:v1:25544" not in fake_redis.values


@pytest.mark.asyncio
async def test_redis_failure_fails_closed(fake_redis: FakeRedis) -> None:
    fake_redis.fail = True
    cache = make_cache(fake_redis)

    with pytest.raises(CacheUnavailableError) as captured:
        await cache.get_or_load(25544, _never)

    assert captured.value.reason_code == "ORBITINTEL_CACHE_UNAVAILABLE"


async def _return(snapshot: GPSnapshot) -> GPSnapshot:
    return snapshot


async def _never() -> GPSnapshot:
    raise AssertionError("loader must not run")
