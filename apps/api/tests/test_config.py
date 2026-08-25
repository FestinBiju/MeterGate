from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import REPOSITORY_ENV_FILE, Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
API_ROOT = REPOSITORY_ROOT / "apps" / "api"
ENVIRONMENT_VARIABLES = (
    "CORS_ALLOWED_ORIGINS",
    "DATABASE_URL",
    "HEALTHCHECK_TIMEOUT_SECONDS",
    "LOG_LEVEL",
    "POLICY_MAX_TTL_SECONDS",
    "QUOTE_TTL_SECONDS",
    "REDIS_URL",
    "SERVICE_NAME",
    "WEBAUTHN_EXPECTED_ORIGINS",
    "WEBAUTHN_RP_ID",
)


def build_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql://user:password@localhost:5432/metergate",
        "redis_url": "redis://localhost:6379/0",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def clear_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


@pytest.mark.parametrize("working_directory", [REPOSITORY_ROOT, API_ROOT])
def test_repository_dotenv_path_is_absolute_and_independent_of_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    working_directory: Path,
) -> None:
    monkeypatch.chdir(working_directory)

    assert REPOSITORY_ENV_FILE == REPOSITORY_ROOT / ".env"
    assert REPOSITORY_ENV_FILE.is_absolute()
    assert Settings.model_config["env_file"] == REPOSITORY_ENV_FILE


def test_settings_load_values_from_an_isolated_dotenv_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_settings_environment(monkeypatch)
    dotenv_file = tmp_path / "settings.env"
    dotenv_file.write_text(
        "\n".join(
            (
                "DATABASE_URL=postgresql+asyncpg://dotenv-user:dotenv-pass@db:5432/metergate",
                "REDIS_URL=rediss://cache:6380/1",
                "SERVICE_NAME=dotenv-service",
                "LOG_LEVEL=DEBUG",
                "HEALTHCHECK_TIMEOUT_SECONDS=4.5",
                "POLICY_MAX_TTL_SECONDS=7200",
                "QUOTE_TTL_SECONDS=900",
                "CORS_ALLOWED_ORIGINS=https://console.example.com,https://admin.example.com",
                "WEBAUTHN_EXPECTED_ORIGINS=https://console.example.com,https://admin.example.com",
                "WEBAUTHN_RP_ID=example.com",
            )
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=dotenv_file)  # type: ignore[call-arg]

    assert (
        settings.database_url.get_secret_value()
        == "postgresql+asyncpg://dotenv-user:dotenv-pass@db:5432/metergate"
    )
    assert settings.redis_url.get_secret_value() == "rediss://cache:6380/1"
    assert settings.service_name == "dotenv-service"
    assert settings.log_level == "DEBUG"
    assert settings.healthcheck_timeout_seconds == 4.5
    assert settings.policy_max_ttl_seconds == 7200
    assert settings.quote_ttl_seconds == 900
    assert settings.cors_allowed_origins == [
        "https://console.example.com",
        "https://admin.example.com",
    ]


def test_operating_system_environment_takes_precedence_over_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_settings_environment(monkeypatch)
    dotenv_file = tmp_path / "settings.env"
    dotenv_file.write_text(
        "\n".join(
            (
                "DATABASE_URL=postgresql://dotenv-user:dotenv-pass@dotenv-db:5432/metergate",
                "REDIS_URL=redis://dotenv-cache:6379/0",
                "SERVICE_NAME=dotenv-service",
                "POLICY_MAX_TTL_SECONDS=3600",
                "QUOTE_TTL_SECONDS=600",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://environment-user:environment-pass@environment-db:5432/metergate",
    )
    monkeypatch.setenv("REDIS_URL", "rediss://environment-cache:6380/2")
    monkeypatch.setenv("SERVICE_NAME", "environment-service")
    monkeypatch.setenv("POLICY_MAX_TTL_SECONDS", "1800")
    monkeypatch.setenv("QUOTE_TTL_SECONDS", "120")

    settings = Settings(_env_file=dotenv_file)  # type: ignore[call-arg]

    assert (
        settings.database_url.get_secret_value()
        == "postgresql+asyncpg://environment-user:environment-pass@environment-db:5432/metergate"
    )
    assert settings.redis_url.get_secret_value() == "rediss://environment-cache:6380/2"
    assert settings.service_name == "environment-service"
    assert settings.policy_max_ttl_seconds == 1800
    assert settings.quote_ttl_seconds == 120


def test_quote_ttl_has_a_safe_default_and_validated_bounds() -> None:
    assert build_settings().quote_ttl_seconds == 300
    assert build_settings(quote_ttl_seconds=1).quote_ttl_seconds == 1
    assert build_settings(quote_ttl_seconds=86_400).quote_ttl_seconds == 86_400

    with pytest.raises(ValidationError):
        build_settings(quote_ttl_seconds=0)
    with pytest.raises(ValidationError):
        build_settings(quote_ttl_seconds=86_401)


def test_policy_max_ttl_has_a_safe_default_and_validated_bounds() -> None:
    assert build_settings().policy_max_ttl_seconds == 86_400
    assert build_settings(policy_max_ttl_seconds=1).policy_max_ttl_seconds == 1
    assert build_settings(policy_max_ttl_seconds=86_400).policy_max_ttl_seconds == 86_400

    with pytest.raises(ValidationError):
        build_settings(policy_max_ttl_seconds=0)
    with pytest.raises(ValidationError):
        build_settings(policy_max_ttl_seconds=86_401)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgres://user:password@localhost:5432/metergate",
        "postgresql://user:password@localhost:5432/metergate",
        "postgresql+asyncpg://user:password@localhost:5432/metergate",
    ],
)
def test_settings_accept_supported_postgresql_url_schemes(database_url: str) -> None:
    settings = build_settings(database_url=database_url)

    assert settings.database_url.get_secret_value() == database_url


@pytest.mark.parametrize(
    "database_url",
    [
        "mysql://user:password@localhost:3306/metergate",
        "http://localhost:5432/metergate",
        "postgresql+psycopg://user:password@localhost:5432/metergate",
        "postgresql:///metergate",
    ],
)
def test_settings_reject_non_postgresql_or_non_network_urls(database_url: str) -> None:
    with pytest.raises(ValidationError, match="valid PostgreSQL URL"):
        build_settings(database_url=database_url)


@pytest.mark.parametrize(
    ("configured_value", "expected", "rp_id", "expected_webauthn_origins"),
    [
        (
            "http://localhost:3000,http://127.0.0.1:3000",
            ["http://localhost:3000", "http://127.0.0.1:3000"],
            "localhost",
            ["http://localhost:3000"],
        ),
        (
            '["https://console.example.com", "https://admin.example.com/"]',
            ["https://console.example.com", "https://admin.example.com"],
            "example.com",
            ["https://console.example.com", "https://admin.example.com"],
        ),
    ],
)
def test_settings_parse_supported_cors_formats(
    configured_value: str,
    expected: list[str],
    rp_id: str,
    expected_webauthn_origins: list[str],
) -> None:
    settings = build_settings(
        cors_allowed_origins=configured_value,
        webauthn_expected_origins=expected_webauthn_origins,
        webauthn_rp_id=rp_id,
    )

    assert settings.cors_allowed_origins == expected


def test_settings_reject_wildcard_cors_origin() -> None:
    with pytest.raises(ValidationError, match="explicit HTTP origins"):
        build_settings(cors_allowed_origins="*")


def test_settings_mask_connection_urls_in_representation() -> None:
    settings = build_settings(
        database_url="postgresql://private-user:private-password@localhost:5432/metergate"
    )

    assert "private-password" not in repr(settings)
    assert "**********" in repr(settings)
