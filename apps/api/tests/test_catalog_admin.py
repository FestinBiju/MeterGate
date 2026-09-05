"""HTTP authorization regression tests for commercial catalog writes."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.v1.dependencies import (
    OperatorContext,
    get_merchant_application_service,
    get_service_application_service,
    require_authenticated_mutation,
    require_current_account,
    require_recent_operator,
)
from app.db.session import get_session
from app.domain.exceptions import ResourceNotFoundError

WRITES = [
    ("POST", "/api/v1/merchants"),
    ("PATCH", "/api/v1/merchants/mrc_example"),
    ("POST", "/api/v1/merchants/mrc_example/services"),
    ("PATCH", "/api/v1/services/svc_example"),
]


class NoCommerce:
    async def create(self, *_):
        pytest.fail("Unauthorized request reached catalog mutation")

    async def update(self, *_):
        pytest.fail("Unauthorized request reached catalog mutation")


def protected_client(make_client):
    client = make_client()
    client.app.dependency_overrides[get_merchant_application_service] = NoCommerce
    client.app.dependency_overrides[get_service_application_service] = NoCommerce
    return client


@pytest.mark.parametrize(("method", "path"), WRITES)
def test_anonymous_catalog_writes_are_rejected(make_client, method, path):
    response = protected_client(make_client).request(method, path, json={"name": "Changed"})
    assert response.status_code == 401
    assert response.json()["reason_code"] == "AUTH_SESSION_REQUIRED"


@pytest.mark.parametrize(("method", "path"), WRITES)
def test_routine_operator_cannot_change_commercial_terms(make_client, method, path):
    client = protected_client(make_client)
    client.app.dependency_overrides[require_recent_operator] = lambda: OperatorContext(
        account_id="acct_test", role="operator"
    )
    response = client.request(method, path, json={"name": "Changed"})
    assert response.status_code == 403
    assert response.json()["reason_code"] == "CATALOG_ADMIN_REQUIRED"


class RoleSession:
    def __init__(self, role):
        self.role = role

    async def scalar(self, _):
        return self.role


def test_buyer_without_an_admin_assignment_is_denied(make_client):
    client = protected_client(make_client)
    current = SimpleNamespace(
        account=SimpleNamespace(id="acct_test"),
        state=SimpleNamespace(authenticated_at=datetime.now(UTC)),
    )
    client.app.dependency_overrides[require_authenticated_mutation] = lambda: current
    client.app.dependency_overrides[get_session] = lambda: RoleSession(None)
    response = client.patch("/api/v1/services/svc_example", json={"base_price": 1})
    assert response.status_code == 403
    assert response.json()["reason_code"] == "OPERATOR_ROLE_REQUIRED"


@pytest.mark.parametrize("minutes", [10])
def test_stale_admin_cannot_change_terms(make_client, minutes):
    client = protected_client(make_client)
    current = SimpleNamespace(
        account=SimpleNamespace(id="acct_test"),
        state=SimpleNamespace(authenticated_at=datetime.now(UTC) - timedelta(minutes=minutes)),
    )
    client.app.dependency_overrides[require_authenticated_mutation] = lambda: current
    client.app.dependency_overrides[get_session] = lambda: RoleSession(
        SimpleNamespace(role="admin")
    )
    response = client.patch("/api/v1/services/svc_example", json={"base_price": 1})
    assert response.status_code == 403
    assert response.json()["reason_code"] == "AUTH_REAUTH_REQUIRED"


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({}, "AUTH_ORIGIN_NOT_ALLOWED"),
        ({"Origin": "https://untrusted.example"}, "AUTH_ORIGIN_NOT_ALLOWED"),
        ({"Origin": "http://localhost:3000"}, "AUTH_CSRF_REQUIRED"),
        ({"Origin": "http://localhost:3000", "X-CSRF-Token": "wrong"}, "AUTH_CSRF_INVALID"),
    ],
)
def test_catalog_write_enforces_origin_and_csrf(make_client, headers, reason):
    client = protected_client(make_client)
    client.app.dependency_overrides[require_current_account] = lambda: SimpleNamespace(
        state=SimpleNamespace(csrf_token="expected")
    )
    response = client.patch("/api/v1/services/svc_example", json={"base_price": 1}, headers=headers)
    assert response.status_code == 403
    assert response.json()["reason_code"] == reason


def test_recent_admin_reaches_validated_management_and_retry_is_safe(make_client):
    client = make_client()
    client.app.dependency_overrides[require_recent_operator] = lambda: OperatorContext(
        account_id="acct_test", role="admin"
    )
    calls = []

    class Service:
        async def update(self, service_id, payload):
            calls.append((service_id, payload.base_price))
            raise ResourceNotFoundError("Service", service_id)

    client.app.dependency_overrides[get_service_application_service] = Service
    for _ in range(2):
        response = client.patch("/api/v1/services/svc_missing", json={"base_price": 500})
        assert response.status_code == 404
    assert calls == [("svc_missing", 500)] * 2
    invalid = client.patch("/api/v1/services/svc_missing", json={"base_price": 1.5})
    assert invalid.status_code == 422
    assert len(calls) == 2


def test_public_merchant_read_does_not_require_admin(make_client):
    client = make_client()

    class Merchant:
        async def get(self, merchant_id):
            raise ResourceNotFoundError("Merchant", merchant_id)

    client.app.dependency_overrides[get_merchant_application_service] = Merchant
    assert client.get("/api/v1/merchants/mrc_missing").status_code == 404
