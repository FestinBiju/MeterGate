from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1.dependencies import get_quote_application_service
from app.domain.exceptions import (
    InactiveMerchantError,
    InactiveServiceError,
    QuoteInputIssue,
    QuoteInputValidationError,
    ResourceNotFoundError,
    ServiceConfigurationError,
)
from app.schemas.quotes import (
    QuoteCreate,
    QuoteFulfillment,
    QuoteMerchant,
    QuotePricing,
    QuoteResponse,
    QuoteService,
)


def quote_response(input_value: Any = None) -> QuoteResponse:
    issued_at = datetime(2026, 8, 25, 12, tzinfo=UTC)
    return QuoteResponse(
        id="qte_00000000000000000000000001",
        merchant=QuoteMerchant(
            id="mrc_00000000000000000000000001",
            slug="orbitintel",
            name="OrbitIntel",
        ),
        service=QuoteService(
            id="svc_00000000000000000000000001",
            slug="orbital-risk-report",
            name="Orbital Risk Report",
            service_type="report",
            input_schema={"type": "null"},
            output_schema={"type": "object"},
            output_content_type="application/json",
        ),
        input=input_value,
        input_hash=f"sha256:{'1' * 64}",
        pricing=QuotePricing(amount=500, currency="INR", purchase_type="one_time"),
        fulfillment=QuoteFulfillment(maximum_seconds=30, refund_on_failure=True),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=5),
        state="active",
        quote_hash=f"sha256:{'2' * 64}",
    )


class StubQuoteApplicationService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.last_payload: QuoteCreate | None = None

    async def create(self, payload: QuoteCreate) -> QuoteResponse:
        if self.error is not None:
            raise self.error
        self.last_payload = payload
        return quote_response(payload.input)

    async def get(self, quote_id: str) -> QuoteResponse:
        if self.error is not None:
            raise self.error
        return quote_response({"quote_id": quote_id})


def client_with_quote_service(
    make_client: Callable[..., TestClient],
    application_service: StubQuoteApplicationService,
) -> TestClient:
    client = make_client()
    client.app.dependency_overrides[get_quote_application_service] = lambda: application_service
    return client


def test_quote_routes_expose_post_and_get_only(
    make_client: Callable[..., TestClient],
) -> None:
    client = client_with_quote_service(make_client, StubQuoteApplicationService())
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths["/api/v1/quotes"]) == {"post"}
    assert set(paths["/api/v1/quotes/{quote_id}"]) == {"get"}
    assert client.patch("/api/v1/quotes/qte_example", json={}).status_code == 405


def test_quote_request_requires_input_allows_root_null_and_forbids_commercial_terms(
    make_client: Callable[..., TestClient],
) -> None:
    application_service = StubQuoteApplicationService()
    client = client_with_quote_service(make_client, application_service)
    service_id = "svc_00000000000000000000000001"

    created = client.post("/api/v1/quotes", json={"service_id": service_id, "input": None})
    missing = client.post("/api/v1/quotes", json={"service_id": service_id})
    overridden = client.post(
        "/api/v1/quotes",
        json={"service_id": service_id, "input": None, "amount": 1, "currency": "USD"},
    )

    assert created.status_code == 201
    assert created.json()["input"] is None
    assert application_service.last_payload == QuoteCreate(service_id=service_id, input=None)
    assert missing.status_code == 422
    assert overridden.status_code == 422
    assert {item["type"] for item in overridden.json()["detail"]} == {"extra_forbidden"}


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (InactiveMerchantError("mrc_example"), "MERCHANT_NOT_ACTIVE"),
        (InactiveServiceError("svc_example"), "SERVICE_NOT_ACTIVE"),
        (ServiceConfigurationError("svc_example"), "SERVICE_CONFIGURATION_INVALID"),
    ],
)
def test_quote_conflicts_have_separate_stable_reason_codes(
    make_client: Callable[..., TestClient],
    error: Exception,
    reason_code: str,
) -> None:
    client = client_with_quote_service(
        make_client,
        StubQuoteApplicationService(error=error),
    )

    response = client.post(
        "/api/v1/quotes",
        json={"service_id": "svc_example", "input": {}},
    )

    assert response.status_code == 409
    assert response.json()["reason_code"] == reason_code


def test_quote_schema_errors_are_sanitized(
    make_client: Callable[..., TestClient],
) -> None:
    error = QuoteInputValidationError((QuoteInputIssue(path=("norad_id",), keyword="type"),))
    client = client_with_quote_service(
        make_client,
        StubQuoteApplicationService(error=error),
    )

    response = client.post(
        "/api/v1/quotes",
        json={"service_id": "svc_example", "input": {"norad_id": "secret-value"}},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "service_input_invalid",
                "loc": ["body", "input", "norad_id"],
                "msg": "Input does not satisfy the service schema",
                "ctx": {"keyword": "type"},
            }
        ]
    }
    assert "secret-value" not in response.text


def test_unknown_quote_and_service_use_safe_not_found_mapping(
    make_client: Callable[..., TestClient],
) -> None:
    client = client_with_quote_service(
        make_client,
        StubQuoteApplicationService(error=ResourceNotFoundError("Quote", "qte_missing")),
    )

    response = client.get("/api/v1/quotes/qte_missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Quote 'qte_missing' was not found"}
