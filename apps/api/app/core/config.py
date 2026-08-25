"""Validated environment-based configuration."""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPOSITORY_ENV_FILE = Path(__file__).resolve().parents[4] / ".env"
_POSTGRESQL_URL_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+asyncpg"})
_RP_ID_PATTERN = re.compile(
    r"^(?=.{1,253}\Z)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


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
    quote_ttl_seconds: int = Field(default=300, ge=1, le=86_400)
    policy_max_ttl_seconds: int = Field(default=86_400, ge=1, le=86_400)
    webauthn_rp_id: str = Field(default="localhost", min_length=1, max_length=253)
    webauthn_rp_name: str = Field(default="MeterGate", min_length=1, max_length=128)
    webauthn_expected_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    webauthn_challenge_ttl_seconds: int = Field(default=300, ge=1, le=600)
    authorization_ttl_seconds: int = Field(default=120, ge=1, le=600)
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

    @field_validator("webauthn_rp_id", mode="after")
    @classmethod
    def validate_webauthn_rp_id(cls, value: str) -> str:
        """Require a bare WebAuthn relying-party domain, never a URL or wildcard."""
        normalized = value.strip().lower().rstrip(".")
        if _RP_ID_PATTERN.fullmatch(normalized) is None:
            raise ValueError("WEBAUTHN_RP_ID must be a valid domain without scheme or port")
        return normalized

    @field_validator("webauthn_rp_name", mode="after")
    @classmethod
    def validate_webauthn_rp_name(cls, value: str) -> str:
        """Reject visually empty relying-party display names."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("WEBAUTHN_RP_NAME must not be blank")
        return normalized

    @field_validator("webauthn_expected_origins", mode="before")
    @classmethod
    def parse_webauthn_expected_origins(cls, value: object) -> list[str]:
        """Parse an explicit WebAuthn origin allowlist and normalize trailing slashes."""
        raw_origins: object = value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    raw_origins = json.loads(stripped)
                except json.JSONDecodeError as error:
                    raise ValueError("WEBAUTHN_EXPECTED_ORIGINS contains invalid JSON") from error
            else:
                raw_origins = [item.strip() for item in stripped.split(",") if item.strip()]

        if not isinstance(raw_origins, list) or not raw_origins:
            raise ValueError("WEBAUTHN_EXPECTED_ORIGINS must contain at least one origin")

        origins: list[str] = []
        for raw_origin in raw_origins:
            if not isinstance(raw_origin, str):
                raise ValueError("WEBAUTHN_EXPECTED_ORIGINS values must be strings")
            origin = raw_origin.rstrip("/")
            parsed = urlsplit(origin)
            try:
                parsed_port = parsed.port
            except ValueError as error:
                raise ValueError("WEBAUTHN_EXPECTED_ORIGINS contains an invalid port") from error
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname != "localhost")
            ):
                raise ValueError(
                    "WEBAUTHN_EXPECTED_ORIGINS must contain explicit HTTPS origins "
                    "or HTTP localhost"
                )
            default_port = 80 if parsed.scheme == "http" else 443
            authority = parsed.hostname.lower()
            if parsed_port is not None and parsed_port != default_port:
                authority = f"{authority}:{parsed_port}"
            normalized = f"{parsed.scheme.lower()}://{authority}"
            if normalized in origins:
                raise ValueError("WEBAUTHN_EXPECTED_ORIGINS must not contain duplicates")
            origins.append(normalized)
        return origins

    @model_validator(mode="after")
    def validate_webauthn_origin_scope(self) -> Self:
        """Ensure every configured browser origin is within the RP ID's domain scope."""
        for origin in self.webauthn_expected_origins:
            hostname = urlsplit(origin).hostname
            if hostname is None or not (
                hostname == self.webauthn_rp_id or hostname.endswith(f".{self.webauthn_rp_id}")
            ):
                raise ValueError(
                    "WEBAUTHN_EXPECTED_ORIGINS hosts must equal or be subdomains of WEBAUTHN_RP_ID"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance per process."""
    return Settings()  # type: ignore[call-arg]
