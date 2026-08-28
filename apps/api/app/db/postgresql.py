"""Minimal PostgreSQL connectivity probe."""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import asyncpg


class PostgreSQLConnection(Protocol):
    async def fetchval(self, query: str) -> Any: ...

    async def close(self) -> None: ...


PostgreSQLConnector = Callable[..., Awaitable[PostgreSQLConnection]]


async def check_postgresql(
    database_url: str,
    *,
    connect: PostgreSQLConnector = asyncpg.connect,
) -> None:
    """Execute a read-only scalar query and always close the connection."""
    connection = await connect(dsn=_to_asyncpg_dsn(database_url))
    try:
        if await connection.fetchval("SELECT 1") != 1:
            raise RuntimeError("PostgreSQL returned an unexpected readiness result")
    finally:
        await connection.close()


def _to_asyncpg_dsn(database_url: str) -> str:
    """Translate only SQLAlchemy's asyncpg scheme into asyncpg's native scheme."""
    parsed = urlsplit(database_url)
    if parsed.scheme == "postgresql+asyncpg":
        return urlunsplit(parsed._replace(scheme="postgresql"))
    return database_url
