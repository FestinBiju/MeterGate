from collections.abc import Callable

from fastapi.testclient import TestClient

from app.api.v1.dependencies import get_merchant_application_service
from app.domain.exceptions import ResourceNotFoundError, SlugConflictError


class FailingMerchantApplicationService:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def create(self, _: object) -> None:
        raise self._error

    async def get(self, _: str) -> None:
        raise self._error


def test_v1_openapi_exposes_exact_management_and_catalog_routes(
    make_client: Callable[..., TestClient],
) -> None:
    paths = make_client().get("/openapi.json").json()["paths"]

    expected_methods = {
        "/api/v1/merchants": {"get", "post"},
        "/api/v1/merchants/{merchant_id}": {"get", "patch"},
        "/api/v1/merchants/{merchant_id}/services": {"get", "post"},
        "/api/v1/services/{service_id}": {"get", "patch"},
        "/api/v1/catalog": {"get"},
        "/api/v1/catalog/services/{service_id}": {"get"},
    }

    for path, methods in expected_methods.items():
        assert path in paths
        assert set(paths[path]) == methods


def test_domain_requests_reject_extra_fields_before_mutation(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().post(
        "/api/v1/merchants",
        json={
            "slug": "orbitintel",
            "name": "OrbitIntel",
            "description": "Synthetic orbital intelligence.",
            "internal_secret": "must-not-be-accepted",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


def test_service_request_rejects_non_object_schema(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().post(
        "/api/v1/merchants/mrc_example/services",
        json={
            "slug": "orbital-risk-report",
            "name": "Orbital Risk Report",
            "description": "Generate a synthetic orbital-risk analysis.",
            "service_type": "report",
            "purchase_type": "one_time",
            "currency": "INR",
            "base_price": 500,
            "input_schema": [],
            "output_schema": {},
            "output_content_type": "application/json",
            "maximum_fulfillment_seconds": 30,
            "refund_on_fulfillment_failure": True,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "input_schema"


def test_patch_request_rejects_empty_and_explicit_null_documents(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()

    empty_response = client.patch("/api/v1/services/svc_example", json={})
    null_response = client.patch(
        "/api/v1/services/svc_example",
        json={"description": None},
    )

    assert empty_response.status_code == 422
    assert null_response.status_code == 422


def test_cors_preflight_allows_domain_api_methods(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().options(
        "/api/v1/services/svc_example",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PATCH",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert set(response.headers["access-control-allow-methods"].split(", ")) == {
        "GET",
        "PATCH",
        "POST",
    }


def test_domain_exceptions_map_to_safe_http_errors(
    make_client: Callable[..., TestClient],
) -> None:
    not_found_client = make_client()
    not_found_client.app.dependency_overrides[get_merchant_application_service] = lambda: (
        FailingMerchantApplicationService(ResourceNotFoundError("Merchant", "mrc_missing"))
    )

    not_found = not_found_client.get("/api/v1/merchants/mrc_missing")

    assert not_found.status_code == 404
    assert not_found.json() == {"detail": "Merchant 'mrc_missing' was not found"}

    conflict_client = make_client()
    conflict_client.app.dependency_overrides[get_merchant_application_service] = lambda: (
        FailingMerchantApplicationService(SlugConflictError("Merchant", "orbitintel"))
    )

    conflict = conflict_client.post(
        "/api/v1/merchants",
        json={
            "slug": "orbitintel",
            "name": "OrbitIntel",
            "description": "Synthetic orbital intelligence.",
        },
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "Merchant slug 'orbitintel' already exists"}
