"""Validated environment-based configuration."""

import json
import math
import re
from functools import lru_cache
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
REPOSITORY_ENV_FILE = REPOSITORY_ROOT / ".env"
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
    app_env: Literal["development", "staging"] = "development"
    demo_mode: bool = False
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
    human_presence_challenge_ttl_seconds: int = Field(default=120, ge=30, le=300)
    human_presence_ttl_seconds: int = Field(default=120, ge=30, le=300)
    auth_session_ttl_seconds: int = Field(default=3_600, ge=60, le=86_400)
    auth_reauth_max_age_seconds: int = Field(default=300, ge=1, le=3_600)
    operator_reauth_max_age_seconds: int = Field(default=180, ge=1, le=3_600)
    outbox_stuck_seconds: int = Field(default=300, ge=30, le=86_400)
    refund_pending_alert_seconds: int = Field(default=300, ge=30, le=86_400)
    refund_uncertain_alert_seconds: int = Field(default=60, ge=10, le=86_400)
    reconciliation_alert_seconds: int = Field(default=60, ge=10, le=86_400)
    webhook_lag_alert_seconds: int = Field(default=120, ge=10, le=86_400)
    worker_heartbeat_stale_seconds: int = Field(default=30, ge=5, le=3_600)
    operator_mutation_rate_limit: int = Field(default=10, ge=1, le=100)
    operator_reconciliation_rate_limit: int = Field(default=5, ge=1, le=100)
    operator_rate_limit_window_seconds: int = Field(default=60, ge=10, le=3_600)
    mcp_agent_session_default_ttl_seconds: int = Field(default=900, ge=60, le=3_600)
    mcp_agent_session_max_ttl_seconds: int = Field(default=3_600, ge=60, le=86_400)
    mcp_rate_limit_window_seconds: int = Field(default=60, ge=10, le=3_600)
    mcp_catalog_rate_limit: int = Field(default=60, ge=1, le=1_000)
    mcp_status_rate_limit: int = Field(default=30, ge=1, le=1_000)
    mcp_mutation_rate_limit: int = Field(default=10, ge=1, le=100)
    mcp_frontend_base_url: str = "http://localhost:3000"
    auth_cookie_name: str = Field(default="metergate_session", min_length=1, max_length=128)
    auth_cookie_secure: bool = False
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    auth_cookie_domain: str | None = None
    auth_cookie_path: str = Field(default="/api/v1", min_length=1, max_length=256)
    payments_enabled: bool = False
    razorpay_mode: Literal["test"] = "test"
    razorpay_key_id: SecretStr | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None
    razorpay_previous_webhook_secret: SecretStr | None = None
    razorpay_webhook_max_age_seconds: int = Field(default=300, ge=60, le=900)
    razorpay_webhook_max_body_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    razorpay_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    razorpay_read_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    razorpay_provider_max_concurrency: int = Field(default=4, ge=1, le=32)
    razorpay_order_recovery_age_seconds: int = Field(default=15, ge=5, le=300)
    refunds_enabled: bool = False
    refund_worker_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    refund_outbox_lease_seconds: int = Field(default=30, ge=5, le=900)
    refund_outbox_retry_base_seconds: int = Field(default=5, ge=1, le=300)
    refund_outbox_retry_max_seconds: int = Field(default=300, ge=1, le=3_600)
    fulfillment_enabled: bool = False
    entitlement_ttl_seconds: int = Field(default=600, ge=60, le=86_400)
    entitlement_token_ttl_seconds: int = Field(default=300, ge=30, le=600)
    entitlement_token_secret: SecretStr | None = None
    entitlement_worker_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    entitlement_outbox_lease_seconds: int = Field(default=30, ge=5, le=900)
    entitlement_outbox_retry_base_seconds: int = Field(default=5, ge=1, le=300)
    entitlement_outbox_retry_max_seconds: int = Field(default=300, ge=1, le=3_600)
    fulfillment_max_attempts: int = Field(default=3, ge=1, le=10)
    fulfillment_execution_lease_seconds: int = Field(default=30, ge=5, le=900)
    fulfillment_max_result_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    orbitintel_base_url: str = "http://127.0.0.1:8100"
    orbitintel_shared_secret: SecretStr | None = None
    orbitintel_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    orbitintel_read_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
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

    @field_validator("orbitintel_base_url", mode="after")
    @classmethod
    def validate_orbitintel_base_url(cls, value: str) -> str:
        """Require a private-development HTTP origin or a production HTTPS origin."""
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ORBITINTEL_BASE_URL must be an explicit HTTP(S) origin")
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
            "orbitintel",
        }:
            raise ValueError("ORBITINTEL_BASE_URL HTTP is allowed only for private local hosts")
        return normalized

    @field_validator("mcp_frontend_base_url", mode="after")
    @classmethod
    def validate_mcp_frontend_base_url(cls, value: str) -> str:
        """Require one explicit browser origin for server-generated handoff links."""
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname != "localhost")
        ):
            raise ValueError("MCP_FRONTEND_BASE_URL must be HTTPS or an HTTP localhost origin")
        return normalized

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

    @field_validator("auth_cookie_name", mode="after")
    @classmethod
    def validate_auth_cookie_name(cls, value: str) -> str:
        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value) is None:
            raise ValueError("AUTH_COOKIE_NAME must be a valid cookie name")
        return value

    @field_validator("auth_cookie_domain", mode="before")
    @classmethod
    def normalize_auth_cookie_domain(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower().lstrip(".")
        return normalized or None

    @field_validator("auth_cookie_domain", mode="after")
    @classmethod
    def validate_auth_cookie_domain(cls, value: str | None) -> str | None:
        if value is not None and _RP_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("AUTH_COOKIE_DOMAIN must be a bare domain")
        return value

    @field_validator("auth_cookie_path", mode="after")
    @classmethod
    def validate_auth_cookie_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or ";" in value
            or any(character.isspace() for character in value)
        ):
            raise ValueError("AUTH_COOKIE_PATH must be an absolute cookie path")
        return value

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
            if origin not in self.cors_allowed_origins:
                raise ValueError(
                    "WEBAUTHN_EXPECTED_ORIGINS must also be present in CORS_ALLOWED_ORIGINS"
                )
        if self.auth_reauth_max_age_seconds > self.auth_session_ttl_seconds:
            raise ValueError("AUTH_REAUTH_MAX_AGE_SECONDS cannot exceed AUTH_SESSION_TTL_SECONDS")
        if self.operator_reauth_max_age_seconds > self.auth_reauth_max_age_seconds:
            raise ValueError(
                "OPERATOR_REAUTH_MAX_AGE_SECONDS cannot exceed AUTH_REAUTH_MAX_AGE_SECONDS"
            )
        if self.mcp_agent_session_default_ttl_seconds > self.mcp_agent_session_max_ttl_seconds:
            raise ValueError(
                "MCP_AGENT_SESSION_DEFAULT_TTL_SECONDS cannot exceed "
                "MCP_AGENT_SESSION_MAX_TTL_SECONDS"
            )
        if self.auth_cookie_samesite == "none" and not self.auth_cookie_secure:
            raise ValueError("AUTH_COOKIE_SAMESITE=none requires AUTH_COOKIE_SECURE=true")
        if self.auth_cookie_name.startswith("__Host-") and (
            not self.auth_cookie_secure
            or self.auth_cookie_domain is not None
            or self.auth_cookie_path != "/"
        ):
            raise ValueError("__Host- cookies require Secure=true, no Domain, and Path=/")
        if self.app_env == "staging":
            if not self.auth_cookie_secure:
                raise ValueError("AUTH_COOKIE_SECURE=true is required in staging")
            if any(urlsplit(origin).scheme != "https" for origin in self.webauthn_expected_origins):
                raise ValueError("Staging WebAuthn origins must use HTTPS")
            if any(urlsplit(origin).scheme != "https" for origin in self.cors_allowed_origins):
                raise ValueError("Staging CORS origins must use HTTPS")
            if urlsplit(self.mcp_frontend_base_url).scheme != "https":
                raise ValueError("MCP_FRONTEND_BASE_URL must use HTTPS in staging")
        if self.razorpay_key_id is not None:
            key_id = self.razorpay_key_id.get_secret_value()
            if re.fullmatch(r"rzp_test_[A-Za-z0-9]{8,64}", key_id) is None:
                raise ValueError("RAZORPAY_KEY_ID must be a Razorpay Test Mode key")
        if self.payments_enabled:
            if self.razorpay_mode != "test":
                raise ValueError("Only Razorpay Test Mode is supported")
            if self.razorpay_key_id is None:
                raise ValueError("RAZORPAY_KEY_ID is required when payments are enabled")
            for setting_name, secret in (
                ("RAZORPAY_KEY_SECRET", self.razorpay_key_secret),
                ("RAZORPAY_WEBHOOK_SECRET", self.razorpay_webhook_secret),
            ):
                secret_value = secret.get_secret_value() if secret is not None else ""
                if (
                    not 8 <= len(secret_value) <= 256
                    or secret_value != secret_value.strip()
                    or "\x00" in secret_value
                ):
                    raise ValueError(
                        f"{setting_name} must contain 8 to 256 valid characters when payments are enabled"
                    )
        if self.razorpay_previous_webhook_secret is not None:
            previous_secret = self.razorpay_previous_webhook_secret.get_secret_value()
            if (
                not 8 <= len(previous_secret) <= 256
                or previous_secret != previous_secret.strip()
                or "\x00" in previous_secret
            ):
                raise ValueError(
                    "RAZORPAY_PREVIOUS_WEBHOOK_SECRET must contain 8 to 256 valid characters"
                )
            current_secret = (
                self.razorpay_webhook_secret.get_secret_value()
                if self.razorpay_webhook_secret is not None
                else None
            )
            if current_secret is not None and compare_digest(previous_secret, current_secret):
                raise ValueError(
                    "RAZORPAY_PREVIOUS_WEBHOOK_SECRET must differ from RAZORPAY_WEBHOOK_SECRET"
                )
        if self.entitlement_outbox_retry_base_seconds > (self.entitlement_outbox_retry_max_seconds):
            raise ValueError(
                "ENTITLEMENT_OUTBOX_RETRY_BASE_SECONDS cannot exceed "
                "ENTITLEMENT_OUTBOX_RETRY_MAX_SECONDS"
            )
        if self.refund_outbox_retry_base_seconds > self.refund_outbox_retry_max_seconds:
            raise ValueError(
                "REFUND_OUTBOX_RETRY_BASE_SECONDS cannot exceed REFUND_OUTBOX_RETRY_MAX_SECONDS"
            )
        if self.refunds_enabled and not self.payments_enabled:
            raise ValueError("REFUNDS_ENABLED requires PAYMENTS_ENABLED=true")
        if self.refunds_enabled:
            provider_operation_budget = (
                self.razorpay_connect_timeout_seconds + self.razorpay_read_timeout_seconds + 1.0
            )
            # A fresh refund performs an exact-receipt lookup before creation,
            # so the fenced lease must cover both bounded provider operations.
            minimum_refund_lease = math.ceil(2 * provider_operation_budget + 2.0)
            if self.refund_outbox_lease_seconds < minimum_refund_lease:
                raise ValueError(
                    "REFUND_OUTBOX_LEASE_SECONDS must cover the bounded Razorpay "
                    f"refund operation budget ({minimum_refund_lease} seconds)"
                )
        if self.payments_enabled and self.fulfillment_enabled:
            provider_operation_budget = (
                self.razorpay_connect_timeout_seconds + self.razorpay_read_timeout_seconds + 1.0
            )
            minimum_value_release_lease = math.ceil(2 * provider_operation_budget + 2.0)
            if self.entitlement_outbox_lease_seconds < minimum_value_release_lease:
                raise ValueError(
                    "ENTITLEMENT_OUTBOX_LEASE_SECONDS must cover the bounded Razorpay "
                    f"value-release proof budget ({minimum_value_release_lease} seconds)"
                )
            if self.fulfillment_execution_lease_seconds < minimum_value_release_lease:
                raise ValueError(
                    "FULFILLMENT_EXECUTION_LEASE_SECONDS must cover the bounded Razorpay "
                    f"value-release proof budget ({minimum_value_release_lease} seconds)"
                )
        if self.fulfillment_enabled:
            for setting_name, secret in (
                ("ENTITLEMENT_TOKEN_SECRET", self.entitlement_token_secret),
                ("ORBITINTEL_SHARED_SECRET", self.orbitintel_shared_secret),
            ):
                secret_value = secret.get_secret_value() if secret is not None else ""
                if (
                    len(secret_value.encode("utf-8")) < 32
                    or len(secret_value) > 512
                    or secret_value != secret_value.strip()
                    or "\x00" in secret_value
                    or secret_value.lower() in {"change-me", "replace-me"}
                ):
                    raise ValueError(
                        f"{setting_name} must contain at least 32 bytes of non-placeholder "
                        "secret material when fulfillment is enabled"
                    )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return repository-root dotenv settings shared by every runtime entrypoint."""
    return Settings(_env_file=REPOSITORY_ENV_FILE)  # type: ignore[call-arg]
