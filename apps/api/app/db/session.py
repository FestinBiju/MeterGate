"""Async SQLAlchemy engine and request-scoped session management."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


class Database:
    """Own an async engine and its configured session factory."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            autoflush=False,
            expire_on_commit=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session without implicitly committing application work."""
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        """Release all pooled database connections owned by this instance."""
        await self.engine.dispose()


def create_database(settings: Settings) -> Database:
    """Create an async database without opening a connection or creating tables."""
    engine = create_async_engine(
        make_async_database_url(settings.database_url.get_secret_value()),
        hide_parameters=True,
        pool_pre_ping=True,
    )
    return Database(engine)


def make_async_database_url(database_url: str) -> URL:
    """Normalize accepted PostgreSQL URLs to SQLAlchemy's asyncpg driver."""
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        return url.set(drivername="postgresql+asyncpg")
    if url.drivername != "postgresql+asyncpg":
        raise ValueError("DATABASE_URL must use PostgreSQL with the asyncpg driver")
    return url


def get_database(request: Request) -> Database:
    """Resolve the application-scoped database for dependency injection."""
    database = getattr(request.app.state, "database", None)
    if not isinstance(database, Database):
        raise RuntimeError("Application database is not configured")
    return database


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Provide one SQLAlchemy session for the lifetime of an API request."""
    async with get_database(request).session() as session:
        yield session
