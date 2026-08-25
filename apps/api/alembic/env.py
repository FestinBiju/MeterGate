"""Alembic environment for async CLI use and injected test connections."""

import asyncio
from logging.config import fileConfig

from sqlalchemy import URL, Connection, pool
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from alembic import context
from app.core.config import get_settings
from app.db.session import make_async_database_url
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def migration_options() -> dict[str, object]:
    return {
        "target_metadata": target_metadata,
        "compare_server_default": True,
        "compare_type": True,
    }


def database_url() -> URL:
    settings = get_settings()
    return make_async_database_url(settings.database_url.get_secret_value())


def run_migrations_offline() -> None:
    """Render migrations without creating an engine or exposing the URL in config."""
    context.configure(
        url=database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **migration_options(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_with_connection(connection: Connection) -> None:
    """Run migrations on a synchronous connection, including AsyncConnection.run_sync."""
    context.configure(connection=connection, **migration_options())
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_with_async_connection(connection: AsyncConnection) -> None:
    await connection.run_sync(run_migrations_with_connection)


async def run_migrations_online_async() -> None:
    """Create and dispose a one-shot async engine for normal Alembic CLI use."""
    connectable = create_async_engine(
        database_url(),
        hide_parameters=True,
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await run_migrations_with_async_connection(connection)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    injected_connection = config.attributes.get("connection")
    if isinstance(injected_connection, AsyncConnection):
        asyncio.run(run_migrations_with_async_connection(injected_connection))
    elif isinstance(injected_connection, Connection):
        run_migrations_with_connection(injected_connection)
    elif injected_connection is not None:
        raise TypeError("Alembic connection attribute must be a SQLAlchemy Connection")
    else:
        asyncio.run(run_migrations_online_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
