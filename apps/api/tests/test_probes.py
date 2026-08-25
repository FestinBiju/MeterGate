from typing import Any

import pytest

from app.cache.redis import check_redis
from app.db.postgresql import check_postgresql


class FakePostgreSQLConnection:
    def __init__(self, result: Any = 1) -> None:
        self.result = result
        self.queries: list[str] = []
        self.closed = False

    async def fetchval(self, query: str) -> Any:
        self.queries.append(query)
        return self.result

    async def close(self) -> None:
        self.closed = True


class FakeRedisClient:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.closed = False

    async def ping(self) -> bool:
        return self.result

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_postgresql_probe_executes_scalar_query_and_closes() -> None:
    connection = FakePostgreSQLConnection()
    received_dsn: str | None = None

    async def connect(*, dsn: str) -> FakePostgreSQLConnection:
        nonlocal received_dsn
        received_dsn = dsn
        return connection

    database_url = "postgresql://user:password@localhost:5432/metergate"
    await check_postgresql(database_url, connect=connect)

    assert received_dsn == database_url
    assert connection.queries == ["SELECT 1"]
    assert connection.closed is True


@pytest.mark.asyncio
async def test_postgresql_probe_adapts_sqlalchemy_asyncpg_url() -> None:
    connection = FakePostgreSQLConnection()
    received_dsn: str | None = None

    async def connect(*, dsn: str) -> FakePostgreSQLConnection:
        nonlocal received_dsn
        received_dsn = dsn
        return connection

    await check_postgresql(
        "postgresql+asyncpg://user:p%40ss@127.0.0.1:5433/metergate"
        "?sslmode=require&application_name=meter%20gate",
        connect=connect,
    )

    assert received_dsn == (
        "postgresql://user:p%40ss@127.0.0.1:5433/metergate"
        "?sslmode=require&application_name=meter%20gate"
    )
    assert connection.queries == ["SELECT 1"]
    assert connection.closed is True


@pytest.mark.asyncio
async def test_postgresql_probe_closes_after_unexpected_result() -> None:
    connection = FakePostgreSQLConnection(result=0)

    async def connect(*, dsn: str) -> FakePostgreSQLConnection:
        return connection

    with pytest.raises(RuntimeError, match="unexpected readiness result"):
        await check_postgresql("postgresql://localhost/metergate", connect=connect)

    assert connection.closed is True


@pytest.mark.asyncio
async def test_redis_probe_pings_and_closes() -> None:
    client = FakeRedisClient()
    received_url: str | None = None

    def client_factory(redis_url: str) -> FakeRedisClient:
        nonlocal received_url
        received_url = redis_url
        return client

    await check_redis("redis://localhost:6379/0", client_factory=client_factory)

    assert received_url == "redis://localhost:6379/0"
    assert client.closed is True


@pytest.mark.asyncio
async def test_redis_probe_closes_after_unexpected_response() -> None:
    client = FakeRedisClient(result=False)

    with pytest.raises(RuntimeError, match="unexpected PING response"):
        await check_redis("redis://localhost:6379/0", client_factory=lambda _: client)

    assert client.closed is True
