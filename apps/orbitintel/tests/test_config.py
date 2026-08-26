import pytest
from pydantic import ValidationError

from app.core.config import Settings
from tests.conftest import INTERNAL_TOKEN


def make_settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "orbitintel_environment": "test",
        "orbitintel_shared_secret": INTERNAL_TOKEN,
        "redis_url": "redis://127.0.0.1:6379/15",
        "_env_file": None,
    }
    values.update(changes)
    return Settings(**values)  # type: ignore[arg-type]


def test_settings_accept_safe_defaults() -> None:
    settings = make_settings()

    assert settings.orbitintel_gp_cache_ttl_seconds == 180
    assert settings.celestrak_max_attempts == 3
    assert settings.orbitintel_dev_fault_mode == "none"
    assert "test-token" not in repr(settings.orbitintel_shared_secret)


@pytest.mark.parametrize(
    "changes",
    [
        {"orbitintel_shared_secret": "too-short"},
        {"redis_url": "sqlite:///tmp/cache.db"},
        {"celestrak_user_agent": "bad\nagent-value-that-is-long-enough"},
        {"orbitintel_gp_lock_ttl_ms": 1_000},
        {"orbitintel_idempotency_lock_ttl_seconds": 10},
        {"orbitintel_redis_socket_timeout_seconds": 0},
        {
            "entitlement_ttl_seconds": 86_400,
            "orbitintel_idempotency_ttl_seconds": 86_400,
        },
    ],
)
def test_settings_reject_unsafe_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**changes)


def test_fault_injection_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="fault injection is forbidden"):
        make_settings(
            orbitintel_environment="production",
            orbitintel_dev_fault_mode="retryable",
        )
