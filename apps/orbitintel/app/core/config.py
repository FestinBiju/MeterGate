"""Validated root-environment configuration for the private merchant service."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
REPOSITORY_ENV_FILE = REPOSITORY_ROOT / ".env"


class Settings(BaseSettings):
    """OrbitIntel settings loaded from MeterGate's repository-root dotenv file."""

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    orbitintel_environment: Literal["development", "test", "production"] = "development"
    orbitintel_shared_secret: SecretStr
    redis_url: SecretStr
    orbitintel_redis_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    orbitintel_redis_socket_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    orbitintel_redis_health_check_interval_seconds: int = Field(default=30, ge=1, le=300)
    orbitintel_gp_cache_ttl_seconds: int = Field(default=180, ge=30, le=900)
    orbitintel_gp_lock_ttl_ms: int = Field(default=30_000, ge=1_000, le=120_000)
    orbitintel_gp_singleflight_wait_seconds: float = Field(default=8.0, gt=0, le=30)
    orbitintel_gp_singleflight_poll_seconds: float = Field(default=0.05, gt=0, le=1)
    orbitintel_idempotency_ttl_seconds: int = Field(default=86_400, ge=600, le=604_800)
    entitlement_ttl_seconds: int = Field(default=600, ge=60, le=86_400)
    orbitintel_idempotency_lock_ttl_seconds: int = Field(default=60, ge=10, le=300)
    orbitintel_idempotency_wait_seconds: float = Field(default=5.0, gt=0, le=30)
    orbitintel_idempotency_poll_seconds: float = Field(default=0.05, gt=0, le=1)
    orbitintel_result_max_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    orbitintel_request_max_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    celestrak_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    celestrak_read_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    celestrak_max_response_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)
    celestrak_max_attempts: int = Field(default=3, ge=1, le=4)
    celestrak_retry_base_delay_seconds: float = Field(default=0.1, ge=0, le=2)
    celestrak_user_agent: str = Field(
        default="OrbitIntel/0.1 (MeterGate reference merchant; CelesTrak GP client)",
        min_length=16,
        max_length=200,
    )
    orbitintel_dev_fault_mode: Literal["none", "retryable", "permanent"] = "none"

    @field_validator("redis_url", mode="after")
    @classmethod
    def validate_redis_url(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("REDIS_URL must be a valid network Redis URL")
        return value

    @field_validator("orbitintel_shared_secret", mode="after")
    @classmethod
    def validate_internal_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if (
            not 32 <= len(token) <= 512
            or token != token.strip()
            or any(ord(character) < 0x21 or ord(character) == 0x7F for character in token)
        ):
            raise ValueError("ORBITINTEL_SHARED_SECRET must contain 32 to 512 printable characters")
        return value

    @field_validator("celestrak_user_agent", mode="after")
    @classmethod
    def validate_user_agent(cls, value: str) -> str:
        normalized = value.strip()
        if re.search(r"[\x00-\x1f\x7f]", normalized):
            raise ValueError("CELESTRAK_USER_AGENT must not contain control characters")
        return normalized

    @model_validator(mode="after")
    def validate_security_and_time_budgets(self) -> Self:
        if self.orbitintel_environment == "production" and self.orbitintel_dev_fault_mode != "none":
            raise ValueError("OrbitIntel fault injection is forbidden in production")
        maximum_upstream_seconds = self.celestrak_max_attempts * (
            self.celestrak_connect_timeout_seconds + self.celestrak_read_timeout_seconds
        )
        retry_delays = sum(
            self.celestrak_retry_base_delay_seconds * (2**attempt)
            for attempt in range(max(0, self.celestrak_max_attempts - 1))
        )
        required_gp_lock_seconds = maximum_upstream_seconds + retry_delays + 5
        required_execution_lock_seconds = (
            required_gp_lock_seconds + self.orbitintel_gp_singleflight_wait_seconds + 5
        )
        if self.orbitintel_idempotency_lock_ttl_seconds < required_execution_lock_seconds:
            raise ValueError(
                "ORBITINTEL_IDEMPOTENCY_LOCK_TTL_SECONDS must exceed the bounded "
                "CelesTrak retry budget"
            )
        required_idempotency_seconds = (
            self.entitlement_ttl_seconds + self.orbitintel_idempotency_lock_ttl_seconds
        )
        if self.orbitintel_idempotency_ttl_seconds < required_idempotency_seconds:
            raise ValueError(
                "ORBITINTEL_IDEMPOTENCY_TTL_SECONDS must exceed the MeterGate "
                "entitlement recovery window"
            )
        if self.orbitintel_gp_lock_ttl_ms < required_gp_lock_seconds * 1_000:
            raise ValueError(
                "ORBITINTEL_GP_LOCK_TTL_MS must exceed the bounded CelesTrak retry budget"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=REPOSITORY_ENV_FILE)  # type: ignore[call-arg]
