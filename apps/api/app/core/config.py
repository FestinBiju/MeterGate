"""Validated environment-based configuration."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPOSITORY_ENV_FILE = Path(__file__).resolve().parents[4] / ".env"
_POSTGRESQL_URL_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+asyncpg"})


class Settings(BaseSettings):
    """Runtime settings loaded from the environment and local dotenv files."""

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "metergate-api"
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"
    healthcheck_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    database_url: SecretStr
    redis_url: SecretStr
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )

    @field_validator("database_url", mode="after")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        """Require a network PostgreSQL DSN while keeping it masked in diagnostics."""
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in _POSTGRESQL_URL_SCHEMES or not parsed.hostname:
            raise ValueError("DATABASE_URL must be a valid PostgreSQL URL")
        return value

    @field_validator("redis_url", mode="after")
    @classmethod
    def validate_redis_url(cls, value: SecretStr) -> SecretStr:
        """Require a network Redis DSN while keeping it masked in diagnostics."""
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("REDIS_URL must be a valid Redis URL")
        return value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_allowed_origins(cls, value: object) -> list[str]:
        """Accept JSON or comma-separated origins and reject wildcard/path values."""
        raw_origins: object = value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    raw_origins = json.loads(stripped)
                except json.JSONDecodeError as error:
                    raise ValueError("CORS_ALLOWED_ORIGINS contains invalid JSON") from error
            else:
                raw_origins = [item.strip() for item in stripped.split(",") if item.strip()]

        if not isinstance(raw_origins, list) or not raw_origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must contain at least one origin")

        origins: list[str] = []
        for raw_origin in raw_origins:
            if not isinstance(raw_origin, str):
                raise ValueError("CORS_ALLOWED_ORIGINS values must be strings")
            origin = raw_origin.rstrip("/")
            parsed = urlsplit(origin)
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("CORS_ALLOWED_ORIGINS must contain only explicit HTTP origins")
            origins.append(origin)
        return origins


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance per process."""
    return Settings()  # type: ignore[call-arg]
