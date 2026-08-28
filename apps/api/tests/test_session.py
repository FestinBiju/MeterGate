import pytest

from app.core.config import Settings
from app.db.session import create_database, make_async_database_url


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
def test_database_url_is_normalized_for_async_sqlalchemy(scheme: str) -> None:
    url = make_async_database_url(
        f"{scheme}://private-user:private-password@localhost:5432/metergate"
    )

    assert url.drivername == "postgresql+asyncpg"
    assert "private-password" not in str(url)
    assert "***" in str(url)


def test_database_url_rejects_non_asyncpg_drivers() -> None:
    with pytest.raises(ValueError, match="PostgreSQL with the asyncpg driver"):
        make_async_database_url("sqlite:///metergate.db")


@pytest.mark.asyncio
async def test_create_database_configures_sessions_without_connecting() -> None:
    settings = Settings(
        database_url="postgresql://user:password@localhost:5432/metergate",
        redis_url="redis://localhost:6379/0",
        _env_file=None,
    )

    database = create_database(settings)
    try:
        assert database.engine.url.drivername == "postgresql+asyncpg"
        assert database.session_factory.kw["autoflush"] is False
        assert database.session_factory.kw["expire_on_commit"] is False
    finally:
        await database.dispose()
