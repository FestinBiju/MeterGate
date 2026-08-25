from collections.abc import Callable

from fastapi.testclient import TestClient

from app.schemas.health import HealthResponse


def test_health_returns_expected_response(make_client: Callable[..., TestClient]) -> None:
    response = make_client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "metergate-api"}
    assert HealthResponse.model_validate(response.json()).status == "ok"


def test_health_declares_typed_response_schema(make_client: Callable[..., TestClient]) -> None:
    schema = make_client().get("/openapi.json").json()

    response_schema = schema["paths"]["/health"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/HealthResponse"}


def test_health_allows_configured_cors_origin(make_client: Callable[..., TestClient]) -> None:
    response = make_client().get(
        "/health",
        headers={"Origin": "http://localhost:3000"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_health_does_not_allow_unconfigured_cors_origin(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().get(
        "/health",
        headers={"Origin": "https://untrusted.example"},
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
