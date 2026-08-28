"""HTTP 402 and bearer-denial contracts for the protected resource gateway."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.v1 import resources
from app.domain.service_input import validate_normalize_and_hash_service_input
from app.providers.fulfillment import (
    MerchantFulfillmentRequest,
    MerchantFulfillmentResult,
)
from app.schemas.entitlements import (
    PaymentRequiredResponse,
    QuoteLink,
    QuoteRequestDescription,
    ResourceParty,
)
from app.services.capabilities import CapabilityTokenService
from app.services.fulfillments import FulfillmentOperation, FulfillmentResult

_SECRET = "protected-resource-test-secret-with-at-least-32-bytes"
_ACCOUNT_ID = "acct_01M00000000000000000000000"
_ENTITLEMENT_ID = "ent_01M00000000000000000000000"
_TRANSACTION_ID = "txn_01M00000000000000000000000"
_MERCHANT_ID = "mrc_01M00000000000000000000000"
_SERVICE_ID = "svc_01M00000000000000000000000"
_OTHER_SERVICE_ID = "svc_01M00000000000000000000001"
_QUOTE_ID = "qte_01M00000000000000000000000"
_QUOTE_HASH = "sha256:" + "1" * 64
_EXECUTION_ID = "ful_01M00000000000000000000000"
_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "norad_id": {
            "type": "integer",
            "minimum": 1,
            "maximum": 999_999_999,
        }
    },
    "required": ["norad_id"],
    "additionalProperties": False,
}


class _RecordingProvider:
    def __init__(self) -> None:
        self.requests: list[MerchantFulfillmentRequest] = []

    async def execute(
        self,
        request: MerchantFulfillmentRequest,
        *,
        endpoint_path: str,
    ) -> MerchantFulfillmentResult:
        assert endpoint_path == "/internal/v1/fulfillments"
        self.requests.append(request)
        return MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"norad_id": request.input["norad_id"], "status": "available"},
        )


class _ProtectedGatewayFake:
    """Exercise real JWT verification/binding while replacing durable fulfillment."""

    def __init__(
        self,
        _session: object,
        capabilities: CapabilityTokenService,
        provider: _RecordingProvider,
        **_kwargs: object,
    ) -> None:
        self._capabilities = capabilities
        self._provider = provider

    async def execute(
        self,
        *,
        merchant_slug: str,
        service_slug: str,
        input_value: object,
        token: str,
    ) -> FulfillmentOperation:
        claims = self._capabilities.verify(token)
        canonical_input = validate_normalize_and_hash_service_input(
            input_value,
            _INPUT_SCHEMA,
        )
        service_id = {
            ("orbitintel", "orbital-risk-report"): _SERVICE_ID,
            ("orbitintel", "satellite-status-lookup"): _OTHER_SERVICE_ID,
        }[(merchant_slug, service_slug)]
        self._capabilities.require_binding(
            claims,
            entitlement_id=_ENTITLEMENT_ID,
            transaction_id=_TRANSACTION_ID,
            merchant_id=_MERCHANT_ID,
            service_id=service_id,
            quote_id=_QUOTE_ID,
            quote_hash=_QUOTE_HASH,
            input_hash=canonical_input.input_hash,
        )
        merchant_result = await self._provider.execute(
            MerchantFulfillmentRequest(
                fulfillment_execution_id=_EXECUTION_ID,
                service_id=service_id,
                service_slug=service_slug,
                input=canonical_input.value,
                input_hash=canonical_input.input_hash,
            ),
            endpoint_path="/internal/v1/fulfillments",
        )
        completed_at = datetime.now(UTC)
        result = FulfillmentResult(
            execution_id=_EXECUTION_ID,
            entitlement_id=_ENTITLEMENT_ID,
            result_content_type=merchant_result.result_content_type,
            result=merchant_result.result,
            result_hash="sha256:" + "3" * 64,
            result_size_bytes=39,
            replayed_result=False,
            completed_at=completed_at,
        )
        return FulfillmentOperation(
            status_code=200,
            result=result,
            execution_id=result.execution_id,
            entitlement_id=result.entitlement_id,
            reason_code="FULFILLMENT_SUCCEEDED",
        )


def _capability(*, issued_at: datetime, norad_id: int = 25_544) -> str:
    canonical_input = validate_normalize_and_hash_service_input(
        {"norad_id": norad_id},
        _INPUT_SCHEMA,
    )
    entitlement = SimpleNamespace(
        id=_ENTITLEMENT_ID,
        account_id=_ACCOUNT_ID,
        transaction_id=_TRANSACTION_ID,
        merchant_id=_MERCHANT_ID,
        service_id=_SERVICE_ID,
        quote_id=_QUOTE_ID,
        quote_hash=_QUOTE_HASH,
        input_hash=canonical_input.input_hash,
        maximum_executions=1,
        expires_at=issued_at + timedelta(minutes=10),
    )
    return (
        CapabilityTokenService(
            _SECRET,
            ttl=timedelta(minutes=5),
            clock=lambda: issued_at,
        )
        .issue(entitlement)
        .token
    )


def _protected_client(
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, _RecordingProvider]:
    client = make_client()
    client.app.state.settings.fulfillment_enabled = True
    client.app.state.settings.entitlement_token_secret = SecretStr(_SECRET)
    provider = _RecordingProvider()
    client.app.state.fulfillment_provider = provider
    monkeypatch.setattr(resources, "FulfillmentApplicationService", _ProtectedGatewayFake)
    return client, provider


async def _challenge(*_: object, **__: object) -> PaymentRequiredResponse:
    return PaymentRequiredResponse(
        merchant=ResourceParty(
            id="mrc_01M00000000000000000000000",
            slug="orbitintel",
            name="OrbitIntel",
        ),
        service=ResourceParty(
            id="svc_01M00000000000000000000000",
            slug="orbital-risk-report",
            name="Orbital Risk Report",
        ),
        quote=QuoteLink(
            request=QuoteRequestDescription(
                service_id="svc_01M00000000000000000000000",
                input={"norad_id": 25544},
            )
        ),
    )


def test_no_capability_returns_machine_readable_402(
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resources, "_payment_required", _challenge)
    response = make_client().post(
        "/api/v1/resources/orbitintel/orbital-risk-report/execute",
        json={"norad_id": 25544},
    )

    assert response.status_code == 402
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "type": "metergate_payment_required",
        "protocol": "metergate/1",
        "merchant": {
            "id": "mrc_01M00000000000000000000000",
            "slug": "orbitintel",
            "name": "OrbitIntel",
        },
        "service": {
            "id": "svc_01M00000000000000000000000",
            "slug": "orbital-risk-report",
            "name": "Orbital Risk Report",
        },
        "quote": {
            "endpoint": "/api/v1/quotes",
            "request": {
                "service_id": "svc_01M00000000000000000000000",
                "input": {"norad_id": 25544},
            },
        },
        "payment_provider": "razorpay",
        "access": {"scheme": "Bearer", "maximum_executions": 1},
    }


def test_public_resource_body_is_rejected_before_unbounded_json_parsing(
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def challenge(*_: object, **__: object) -> PaymentRequiredResponse:
        nonlocal called
        called = True
        return await _challenge()

    monkeypatch.setattr(resources, "_payment_required", challenge)
    response = make_client().post(
        "/api/v1/resources/orbitintel/orbital-risk-report/execute",
        content=b'"' + b"x" * (256 * 1024) + b'"',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["reason_code"] == "RESOURCE_REQUEST_BODY_TOO_LARGE"
    assert response.headers["cache-control"] == "private, no-store"
    assert called is False


def test_public_resource_rejects_malformed_json_with_stable_code(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().post(
        "/api/v1/resources/orbitintel/orbital-risk-report/execute",
        content=b'{"norad_id":',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["reason_code"] == "RESOURCE_REQUEST_BODY_INVALID"


@pytest.mark.parametrize("input_value", [[1, 2], "satellite", 25_544, True, None])
def test_402_preserves_any_json_root_allowed_by_the_service_contract(
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
    input_value: object,
) -> None:
    async def challenge(*_: object, **kwargs: object) -> PaymentRequiredResponse:
        return PaymentRequiredResponse(
            merchant=ResourceParty(id=_MERCHANT_ID, slug="orbitintel", name="OrbitIntel"),
            service=ResourceParty(
                id=_SERVICE_ID,
                slug="orbital-risk-report",
                name="Orbital Risk Report",
            ),
            quote=QuoteLink(
                request=QuoteRequestDescription(
                    service_id=_SERVICE_ID,
                    input=kwargs["input_value"],
                )
            ),
        )

    monkeypatch.setattr(resources, "_payment_required", challenge)
    response = make_client().post(
        "/api/v1/resources/orbitintel/orbital-risk-report/execute",
        content=json.dumps(input_value),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 402
    assert response.json()["quote"]["request"]["input"] == input_value


def test_malformed_capability_is_401_without_database_access(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().post(
        "/api/v1/resources/orbitintel/orbital-risk-report/execute",
        headers={"Authorization": "Basic not-a-capability"},
        json={"norad_id": 25544},
    )

    assert response.status_code == 401
    assert response.json()["reason_code"] == "CAPABILITY_INVALID"
    assert response.headers["www-authenticate"].startswith("Bearer ")


def test_expired_capability_is_denied_at_the_http_boundary(
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, provider = _protected_client(make_client, monkeypatch)
    expired_token = _capability(issued_at=datetime.now(UTC) - timedelta(minutes=10))

    response = client.post(
        "/api/v1/resources/orbitintel/orbital-risk-report/execute",
        headers={"Authorization": f"Bearer {expired_token}"},
        json={"norad_id": 25_544},
    )

    assert response.status_code == 401
    assert response.json()["reason_code"] == "CAPABILITY_EXPIRED"
    assert response.headers["www-authenticate"].startswith("Bearer ")
    assert response.headers["cache-control"] == "private, no-store"
    assert provider.requests == []


def test_fulfillment_kill_switch_blocks_existing_capability_and_result_replay(
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    client.app.state.settings.fulfillment_enabled = False
    client.app.state.settings.entitlement_token_secret = SecretStr(_SECRET)
    provider = _RecordingProvider()
    client.app.state.fulfillment_provider = provider
    monkeypatch.setattr(resources, "FulfillmentApplicationService", _ProtectedGatewayFake)
    token = _capability(issued_at=datetime.now(UTC))

    response = client.post(
        "/api/v1/resources/orbitintel/orbital-risk-report/execute",
        headers={"Authorization": f"Bearer {token}"},
        json={"norad_id": 25_544},
    )

    assert response.status_code == 503
    assert response.json()["reason_code"] == "FULFILLMENT_PROVIDER_UNAVAILABLE"
    assert response.headers["cache-control"] == "private, no-store"
    assert provider.requests == []


def test_valid_capability_cannot_execute_a_different_input(
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, provider = _protected_client(make_client, monkeypatch)
    token = _capability(issued_at=datetime.now(UTC))

    response = client.post(
        "/api/v1/resources/orbitintel/orbital-risk-report/execute",
        headers={"Authorization": f"Bearer {token}"},
        json={"norad_id": 20_580},
    )

    assert response.status_code == 403
    assert response.json()["reason_code"] == "CAPABILITY_RESOURCE_MISMATCH"
    assert response.headers["cache-control"] == "private, no-store"
    assert provider.requests == []


def test_valid_capability_cannot_execute_a_different_service(
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, provider = _protected_client(make_client, monkeypatch)
    token = _capability(issued_at=datetime.now(UTC))

    response = client.post(
        "/api/v1/resources/orbitintel/satellite-status-lookup/execute",
        headers={"Authorization": f"Bearer {token}"},
        json={"norad_id": 25_544},
    )

    assert response.status_code == 403
    assert response.json()["reason_code"] == "CAPABILITY_RESOURCE_MISMATCH"
    assert response.headers["cache-control"] == "private, no-store"
    assert provider.requests == []


def test_valid_exact_capability_executes_the_bound_resource(
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, provider = _protected_client(make_client, monkeypatch)
    token = _capability(issued_at=datetime.now(UTC))

    response = client.post(
        "/api/v1/resources/orbitintel/orbital-risk-report/execute",
        headers={"Authorization": f"Bearer {token}"},
        json={"norad_id": 25_544},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "execution_id": _EXECUTION_ID,
        "entitlement_id": _ENTITLEMENT_ID,
        "result_content_type": "application/json",
        "result": {"norad_id": 25_544, "status": "available"},
        "result_hash": "sha256:" + "3" * 64,
        "result_size_bytes": 39,
        "replayed_result": False,
        "completed_at": response.json()["completed_at"],
    }
    assert len(provider.requests) == 1
    assert provider.requests[0].service_id == _SERVICE_ID
    assert provider.requests[0].service_slug == "orbital-risk-report"
    assert provider.requests[0].input == {"norad_id": 25_544}


def test_cors_preflight_allows_authorization_header(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().options(
        "/api/v1/resources/orbitintel/orbital-risk-report/execute",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed
    assert "content-type" in allowed
