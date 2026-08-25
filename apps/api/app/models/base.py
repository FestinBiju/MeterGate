"""Shared SQLAlchemy declarative types and persistence invariants."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class JSONObject(TypeDecorator[dict[str, Any]]):
    """A JSON object stored as JSONB on PostgreSQL and JSON elsewhere."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(
        self,
        value: dict[str, Any] | None,
        dialect: Any,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("JSON schema columns must contain JSON objects")
        return value

    def process_result_value(
        self,
        value: Any,
        dialect: Any,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("Persisted JSON schema value is not an object")
        return value


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
