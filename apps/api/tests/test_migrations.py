from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command

API_ROOT = Path(__file__).resolve().parents[1]


def test_initial_migration_upgrades_and_downgrades_with_injected_connection() -> None:
    engine = create_engine("sqlite://")
    config = Config(API_ROOT / "alembic.ini")

    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

        inspector = inspect(connection)
        assert set(inspector.get_table_names()) == {
            "alembic_version",
            "merchants",
            "services",
        }
        assert {index["name"] for index in inspector.get_indexes("services")} >= {
            "ix_services_catalog",
            "ix_services_merchant_list",
        }
        service_foreign_key = inspector.get_foreign_keys("services")[0]
        assert service_foreign_key["referred_table"] == "merchants"
        assert service_foreign_key["options"]["ondelete"] == "RESTRICT"

        command.downgrade(config, "base")
        assert inspect(connection).get_table_names() == ["alembic_version"]

    engine.dispose()
