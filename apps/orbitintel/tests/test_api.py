import json
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.errors import CelesTrakUnavailableError
from app.hashing import canonical_json_hash, fulfillment_request_hash
from tests.conftest import (
    FULFILLMENT_ID,
    INTERNAL_TOKEN,
    SECOND_FULFILLMENT_ID,
    SERVICE_ID,
    THIRD_FULFILLMENT_ID,
)
from tests.fakes import FakeGPProvider, FakeRedis


def headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {INTERNAL_TOKEN}"}


def payload(
    *,
    service_slug: str = "satellite-status-lookup",
    norad_id: int = 25544,
) -> dict[str, object]:
    input_value = {"norad_id": norad_id}
    return {
        "service_id": SERVICE_ID,
        "service_slug": service_slug,
        "input": input_value,
        "input_hash": canonical_json_hash(input_value),
    }


def endpoint(execution_id: str = FULFILLMENT_ID) -> str:
    return f"/internal/v1/fulfillments/{execution_id}"


def test_private_fulfillment_requires_exact_bearer(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()

    for authorization in (None, "", "Basic abc", "Bearer wrong-token"):
        request_headers = {} if authorization is None else {"Authorization": authorization}
        response = client.post(endpoint(), headers=request_headers, json=payload())
        assert response.status_code == 401
        assert response.json()["reason_code"] == "ORBITINTEL_AUTH_INVALID"
        assert response.headers["www-authenticate"] == "Bearer"


def test_success_contract_and_duplicate_replay_are_exact(
    make_client: Callable[..., TestClient],
    fake_provider: FakeGPProvider,
) -> None:
    client = make_client()

    first = client.post(endpoint(), headers=headers(), json=payload())
    second = client.post(endpoint(), headers=headers(), json=payload())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content
    assert first.headers["x-orbitintel-idempotent-replay"] == "false"
    assert second.headers["x-orbitintel-idempotent-replay"] == "true"
    assert first.headers["cache-control"] == "private, no-store"
    assert first.headers["x-content-type-options"] == "nosniff"
    body = first.json()
    assert set(body) == {
        "fulfillment_execution_id",
        "service_id",
        "input_hash",
        "result_content_type",
        "result",
    }
    assert body["fulfillment_execution_id"] == FULFILLMENT_ID
    assert body["service_id"] == SERVICE_ID
    assert body["input_hash"] == payload()["input_hash"]
    assert body["result_content_type"] == "application/json"
    assert body["result"]["result_type"] == "satellite_status"
    assert body["result"]["data_source"] == "CelesTrak"
    assert fake_provider.calls == [25544]


@pytest.mark.parametrize("binding", ["service_id", "input_hash", "norad_id"])
def test_replay_revalidates_exact_request_and_result_bindings(
    make_client: Callable[..., TestClient],
    fake_provider: FakeGPProvider,
    fake_redis: FakeRedis,
    binding: str,
) -> None:
    client = make_client()
    first = client.post(endpoint(), headers=headers(), json=payload())
    assert first.status_code == 200
    body = first.json()
    if binding == "service_id":
        body["service_id"] = "svc_01J00000000000000000000001"
    elif binding == "input_hash":
        body["input_hash"] = "sha256:" + "f" * 64
    else:
        body["result"]["norad_id"] = 25545
    fake_redis.values[f"orbitintel:idempotency:result:v1:{FULFILLMENT_ID}"] = json.dumps(
        body,
        separators=(",", ":"),
    ).encode()

    replay = client.post(endpoint(), headers=headers(), json=payload())

    assert replay.status_code == 503
    assert replay.json()["reason_code"] == "ORBITINTEL_IDEMPOTENCY_INTEGRITY_FAILED"
    assert fake_provider.calls == [25544]


def test_all_three_service_contracts_are_real_and_scoped(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()
    cases = (
        (FULFILLMENT_ID, "satellite-status-lookup", "satellite_status"),
        (SECOND_FULFILLMENT_ID, "orbital-risk-report", "orbital_risk_report"),
        (
            THIRD_FULFILLMENT_ID,
            "detailed-orbital-analysis",
            "detailed_orbital_analysis",
        ),
    )

    responses = [
        client.post(
            endpoint(execution_id),
            headers=headers(),
            json=payload(service_slug=service_slug),
        )
        for execution_id, service_slug, _ in cases
    ]

    assert [response.status_code for response in responses] == [200, 200, 200]
    for response, (_, _, result_type) in zip(responses, cases, strict=True):
        assert response.json()["result"]["result_type"] == result_type
    risk = responses[1].json()["result"]
    detailed = responses[2].json()["result"]
    for result in (risk, detailed):
        assert result["assessment_scope"] == "heuristic_orbital_condition"
        assert result["operational_collision_assessment"] is False
        assert result["data_source"] == "CelesTrak"
        assert "not a conjunction warning" in result["disclaimer"]


def test_input_hash_is_recomputed_before_execution(
    make_client: Callable[..., TestClient],
    fake_provider: FakeGPProvider,
) -> None:
    client = make_client()
    request_payload = payload()
    request_payload["input_hash"] = "sha256:" + "0" * 64

    response = client.post(endpoint(), headers=headers(), json=request_payload)

    assert response.status_code == 422
    assert response.json()["reason_code"] == "ORBITINTEL_INPUT_HASH_MISMATCH"
    assert fake_provider.calls == []


def test_execution_id_binding_mismatch_is_rejected_without_reexecution(
    make_client: Callable[..., TestClient],
    fake_provider: FakeGPProvider,
) -> None:
    client = make_client()
    assert client.post(endpoint(), headers=headers(), json=payload()).status_code == 200

    response = client.post(
        endpoint(),
        headers=headers(),
        json=payload(service_slug="orbital-risk-report"),
    )

    assert response.status_code == 409
    assert response.json()["reason_code"] == "ORBITINTEL_IDEMPOTENCY_MISMATCH"
    assert fake_provider.calls == [25544]


@pytest.mark.parametrize(
    "bad_payload",
    [
        {
            "service_id": SERVICE_ID,
            "service_slug": "unknown-service",
            "input": {"norad_id": 25544},
            "input_hash": "sha256:" + "0" * 64,
        },
        {
            **payload(),
            "input": {"norad_id": True},
        },
        {
            **payload(),
            "fault_mode": "none",
        },
    ],
)
def test_invalid_requests_return_one_stable_code(
    make_client: Callable[..., TestClient],
    bad_payload: dict[str, object],
) -> None:
    response = make_client().post(endpoint(), headers=headers(), json=bad_payload)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "OrbitIntel request validation failed",
        "reason_code": "ORBITINTEL_REQUEST_INVALID",
    }


@pytest.mark.parametrize(
    ("mode", "status_code", "reason_code"),
    [
        ("retryable", 503, "ORBITINTEL_FAULT_RETRYABLE"),
        ("permanent", 422, "ORBITINTEL_FAULT_PERMANENT"),
    ],
)
def test_config_only_fault_injection_applies_to_every_call(
    settings: Settings,
    make_client: Callable[..., TestClient],
    fake_provider: FakeGPProvider,
    mode: str,
    status_code: int,
    reason_code: str,
) -> None:
    effective_settings = Settings(
        orbitintel_environment="test",
        orbitintel_shared_secret=INTERNAL_TOKEN,
        redis_url=settings.redis_url,
        orbitintel_dev_fault_mode=mode,
        _env_file=None,
    )
    client = make_client(effective_settings=effective_settings)

    first = client.post(endpoint(), headers=headers(), json=payload())
    second = client.post(endpoint(), headers=headers(), json=payload())

    assert first.status_code == status_code
    assert second.status_code == status_code
    assert first.json()["reason_code"] == reason_code
    assert second.json()["reason_code"] == reason_code
    assert fake_provider.calls == []


def test_temporary_upstream_failure_returns_503(
    make_client: Callable[..., TestClient],
    snapshot: object,
) -> None:
    provider = FakeGPProvider(
        snapshot,  # type: ignore[arg-type]
        error_factory=lambda: CelesTrakUnavailableError(
            "CelesTrak is temporarily unavailable",
            "CELESTRAK_UNAVAILABLE",
        ),
    )
    client = make_client(provider=provider)

    response = client.post(endpoint(), headers=headers(), json=payload())

    assert response.status_code == 503
    assert response.json()["reason_code"] == "CELESTRAK_UNAVAILABLE"
    assert response.headers["retry-after"] == "1"


def test_in_progress_returns_stable_409(
    settings: Settings,
    make_client: Callable[..., TestClient],
    fake_redis: FakeRedis,
) -> None:
    request_payload = payload()
    input_value = request_payload["input"]
    assert isinstance(input_value, dict)
    request_hash = fulfillment_request_hash(
        fulfillment_execution_id=FULFILLMENT_ID,
        service_id=SERVICE_ID,
        service_slug="satellite-status-lookup",
        input_value=input_value,
        input_hash=str(request_payload["input_hash"]),
    )
    fake_redis.values[f"orbitintel:idempotency:binding:v1:{FULFILLMENT_ID}"] = request_hash.encode()
    fake_redis.values[f"orbitintel:idempotency:lock:v1:{FULFILLMENT_ID}"] = b"another-worker"
    effective_settings = Settings(
        orbitintel_environment="test",
        orbitintel_shared_secret=INTERNAL_TOKEN,
        redis_url=settings.redis_url,
        orbitintel_idempotency_wait_seconds=0.002,
        orbitintel_idempotency_poll_seconds=0.001,
        _env_file=None,
    )
    client = make_client(effective_settings=effective_settings)

    response = client.post(endpoint(), headers=headers(), json=request_payload)

    assert response.status_code == 409
    assert response.json()["reason_code"] == "ORBITINTEL_EXECUTION_IN_PROGRESS"
    assert response.headers["retry-after"] == "1"


def test_duplicate_authorization_headers_are_rejected(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()

    response = client.post(
        endpoint(),
        headers=[
            ("Authorization", f"Bearer {INTERNAL_TOKEN}"),
            ("Authorization", f"Bearer {INTERNAL_TOKEN}"),
            ("Content-Type", "application/json"),
        ],
        content=json.dumps(payload()),
    )

    assert response.status_code == 401
    assert response.json()["reason_code"] == "ORBITINTEL_AUTH_INVALID"


@pytest.mark.parametrize(
    "content_length",
    ["not-a-number", "-1", "+1", "1.0", " 1", "1 "],
)
def test_invalid_content_length_is_rejected_before_json_validation(
    make_client: Callable[..., TestClient],
    content_length: str,
) -> None:
    response = make_client().post(
        endpoint(),
        headers={**headers(), "Content-Type": "application/json", "Content-Length": content_length},
        content=b"{",
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "OrbitIntel Content-Length is invalid",
        "reason_code": "ORBITINTEL_REQUEST_INVALID",
    }


def test_duplicate_content_length_is_rejected_before_json_validation(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().post(
        endpoint(),
        headers=[
            ("Authorization", f"Bearer {INTERNAL_TOKEN}"),
            ("Content-Type", "application/json"),
            ("Content-Length", "1"),
            ("Content-Length", "1"),
        ],
        content=b"{",
    )

    assert response.status_code == 422
    assert response.json()["reason_code"] == "ORBITINTEL_REQUEST_INVALID"


def test_declared_oversized_body_is_rejected_before_json_buffering(
    settings: Settings,
    make_client: Callable[..., TestClient],
    fake_provider: FakeGPProvider,
) -> None:
    effective_settings = Settings(
        orbitintel_environment="test",
        orbitintel_shared_secret=INTERNAL_TOKEN,
        redis_url=settings.redis_url,
        orbitintel_request_max_bytes=1_024,
        _env_file=None,
    )
    response = make_client(effective_settings=effective_settings).post(
        endpoint(),
        headers={
            **headers(),
            "Content-Type": "application/json",
            "Content-Length": "1025",
        },
        content=b"{",
    )

    assert response.status_code == 413
    assert response.json()["reason_code"] == "ORBITINTEL_REQUEST_BODY_TOO_LARGE"
    assert fake_provider.calls == []


def test_chunked_oversized_body_is_rejected_while_streaming(
    settings: Settings,
    make_client: Callable[..., TestClient],
    fake_provider: FakeGPProvider,
) -> None:
    effective_settings = Settings(
        orbitintel_environment="test",
        orbitintel_shared_secret=INTERNAL_TOKEN,
        redis_url=settings.redis_url,
        orbitintel_request_max_bytes=1_024,
        _env_file=None,
    )
    chunks = iter((b"{" + b" " * 700, b" " * 700 + b"}"))
    response = make_client(effective_settings=effective_settings).post(
        endpoint(),
        headers={
            **headers(),
            "Content-Type": "application/json",
            "Transfer-Encoding": "chunked",
        },
        content=chunks,
    )

    assert response.status_code == 413
    assert response.json()["reason_code"] == "ORBITINTEL_REQUEST_BODY_TOO_LARGE"
    assert fake_provider.calls == []


def test_authentication_precedes_body_header_and_stream_processing(
    settings: Settings,
    make_client: Callable[..., TestClient],
) -> None:
    effective_settings = Settings(
        orbitintel_environment="test",
        orbitintel_shared_secret=INTERNAL_TOKEN,
        redis_url=settings.redis_url,
        orbitintel_request_max_bytes=1_024,
        _env_file=None,
    )
    response = make_client(effective_settings=effective_settings).post(
        endpoint(),
        headers={
            "Authorization": "Bearer wrong-token",
            "Content-Type": "application/json",
            "Content-Length": "1025",
        },
        content=b"{",
    )

    assert response.status_code == 401
    assert response.json()["reason_code"] == "ORBITINTEL_AUTH_INVALID"


def test_liveness_does_not_depend_on_upstream(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "orbitintel"}
