from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1.dependencies import (
    get_payment_application_service,
    require_authenticated_mutation,
    require_current_account,
)
from app.domain.enums import PaymentTransactionState, PurchaseType
from app.domain.exceptions import (
    PaymentConflictError,
    PaymentExpiredError,
    PaymentIntegrityError,
    PaymentNotFoundError,
    PaymentProviderError,
    PaymentTimeoutError,
    PaymentUnavailableError,
    PaymentVerificationError,
)
from app.schemas.payments import (
    CheckoutVerificationCreate,
    PaymentTransactionCreate,
    PaymentTransactionResponse,
    RazorpayCheckoutConfiguration,
)
from app.services.payments import PaymentOperationResult

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)
ACCOUNT_ID = "acct_00000000000000000000000001"
CURRENT_ACCOUNT_ID = "acct_00000000000000000000000002"
AUTHORIZATION_ID = "aut_00000000000000000000000001"
TRANSACTION_ID = "txn_00000000000000000000000001"
MERCHANT_ID = "mrc_00000000000000000000000001"
SERVICE_ID = "svc_00000000000000000000000001"
ORDER_ID = "order_testOrder123"
PAYMENT_ID = "pay_testPayment123"
SIGNATURE = "a" * 64


def transaction_response(
    *,
    state: PaymentTransactionState = PaymentTransactionState.ORDER_CREATED,
) -> PaymentTransactionResponse:
    has_checkout = state in {
        PaymentTransactionState.ORDER_CREATED,
        PaymentTransactionState.PAYMENT_PENDING,
        PaymentTransactionState.PAYMENT_AUTHORIZED,
    }
    checkout = (
        RazorpayCheckoutConfiguration(
            transaction_id=TRANSACTION_ID,
            provider="razorpay",
            key_id="rzp_test_12345678",
            order_id=ORDER_ID,
            amount=500,
            currency="INR",
            name="OrbitIntel",
            description="Orbital Risk Report",
        )
        if has_checkout
        else None
    )
    return PaymentTransactionResponse(
        reason_code="PAYMENT_TRANSACTION_READY",
        transaction_id=TRANSACTION_ID,
        state=state,
        merchant={"id": MERCHANT_ID, "name": "OrbitIntel"},
        service={"id": SERVICE_ID, "name": "Orbital Risk Report"},
        amount=500,
        currency="INR",
        purchase_type=PurchaseType.ONE_TIME,
        authorization_id=AUTHORIZATION_ID,
        provider="razorpay",
        provider_order_id=ORDER_ID if has_checkout else None,
        provider_order_status="created" if has_checkout else None,
        attempts=[],
        checkout=checkout,
        created_at=NOW,
        paid_at=None,
        last_reconciled_at=None,
    )


class PrincipalOverride:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return SimpleNamespace(account=SimpleNamespace(id=self.account_id))


class StubPaymentService:
    def __init__(
        self,
        *,
        create_status: int = 201,
        verify_status: int = 200,
        reconcile_status: int = 200,
        error: Exception | None = None,
    ) -> None:
        self.create_status = create_status
        self.verify_status = verify_status
        self.reconcile_status = reconcile_status
        self.error = error
        self.calls: list[tuple[object, ...]] = []

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def create_transaction(
        self,
        payload: PaymentTransactionCreate,
        *,
        account_id: str,
    ) -> PaymentOperationResult:
        self._raise()
        self.calls.append(("create", payload, account_id))
        state = (
            PaymentTransactionState.ORDER_CREATION_PENDING
            if self.create_status == 202
            else PaymentTransactionState.ORDER_CREATED
        )
        return PaymentOperationResult(transaction_response(state=state), self.create_status)

    async def get_transaction(
        self,
        transaction_id: str,
        *,
        account_id: str,
    ) -> PaymentTransactionResponse:
        self._raise()
        self.calls.append(("get", transaction_id, account_id))
        return transaction_response()

    async def verify_checkout(
        self,
        transaction_id: str,
        payload: CheckoutVerificationCreate,
        *,
        account_id: str,
    ) -> PaymentOperationResult:
        self._raise()
        self.calls.append(("verify", transaction_id, payload, account_id))
        return PaymentOperationResult(transaction_response(), self.verify_status)

    async def reconcile_transaction(
        self,
        transaction_id: str,
        *,
        account_id: str,
    ) -> PaymentOperationResult:
        self._raise()
        self.calls.append(("reconcile", transaction_id, account_id))
        return PaymentOperationResult(transaction_response(), self.reconcile_status)


def client_with_payment_service(
    make_client: Callable[..., TestClient],
    service: StubPaymentService | None = None,
    *,
    current_account_id: str = ACCOUNT_ID,
    mutation_account_id: str = ACCOUNT_ID,
) -> tuple[TestClient, StubPaymentService, PrincipalOverride, PrincipalOverride]:
    client = make_client()
    payment_service = service or StubPaymentService()
    current = PrincipalOverride(current_account_id)
    mutation = PrincipalOverride(mutation_account_id)
    client.app.dependency_overrides[get_payment_application_service] = lambda: payment_service
    client.app.dependency_overrides[require_current_account] = current
    client.app.dependency_overrides[require_authenticated_mutation] = mutation
    return client, payment_service, current, mutation


def checkout_payload() -> dict[str, str]:
    return {
        "razorpay_payment_id": PAYMENT_ID,
        "razorpay_order_id": ORDER_ID,
        "razorpay_signature": SIGNATURE,
    }


def test_payment_routes_expose_only_the_intended_methods(
    make_client: Callable[..., TestClient],
) -> None:
    client, _, _, _ = client_with_payment_service(make_client)
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths["/api/v1/payment-transactions"]) == {"post"}
    assert set(paths["/api/v1/payment-transactions/{transaction_id}"]) == {"get"}
    assert set(paths["/api/v1/payment-transactions/{transaction_id}/checkout/verify"]) == {"post"}
    assert set(paths["/api/v1/payment-transactions/{transaction_id}/reconcile"]) == {"post"}
    assert set(paths["/api/v1/webhooks/razorpay"]) == {"post"}
    assert client.get("/api/v1/payment-transactions").status_code == 405
    assert (
        client.patch(f"/api/v1/payment-transactions/{TRANSACTION_ID}", json={}).status_code == 405
    )
    assert (
        client.delete(f"/api/v1/payment-transactions/{TRANSACTION_ID}/checkout/verify").status_code
        == 405
    )
    assert client.get("/api/v1/webhooks/razorpay").status_code == 405


@pytest.mark.parametrize(
    ("status_code", "expected_state"),
    [
        (201, "order_created"),
        (200, "order_created"),
        (202, "order_creation_pending"),
    ],
)
def test_create_uses_the_service_dynamic_success_status(
    make_client: Callable[..., TestClient],
    status_code: int,
    expected_state: str,
) -> None:
    service = StubPaymentService(create_status=status_code)
    client, service, _, _ = client_with_payment_service(make_client, service)

    response = client.post(
        "/api/v1/payment-transactions",
        json={"authorization_id": AUTHORIZATION_ID},
    )

    assert response.status_code == status_code
    assert response.json()["state"] == expected_state
    assert service.calls == [
        ("create", PaymentTransactionCreate(authorization_id=AUTHORIZATION_ID), ACCOUNT_ID)
    ]


def test_create_accepts_only_the_authorization_id_and_rejects_client_terms(
    make_client: Callable[..., TestClient],
) -> None:
    client, service, _, _ = client_with_payment_service(make_client)

    for field, value in (("amount", 1), ("currency", "USD")):
        response = client.post(
            "/api/v1/payment-transactions",
            json={"authorization_id": AUTHORIZATION_ID, field: value},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == [
            {
                "type": "extra_forbidden",
                "loc": ["body", field],
                "msg": "Extra inputs are not permitted",
                "input": value,
            }
        ]

    missing = client.post("/api/v1/payment-transactions", json={})
    malformed = client.post(
        "/api/v1/payment-transactions",
        json={"authorization_id": "attacker-controlled"},
    )

    assert missing.status_code == 422
    assert malformed.status_code == 422
    assert service.calls == []


def test_protected_payment_routes_reject_a_missing_session_before_service_work(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()
    service = StubPaymentService()
    client.app.dependency_overrides[get_payment_application_service] = lambda: service

    fetched = client.get(f"/api/v1/payment-transactions/{TRANSACTION_ID}")
    created = client.post(
        "/api/v1/payment-transactions",
        headers={"Origin": "http://localhost:3000", "X-CSRF-Token": "c" * 43},
        json={"authorization_id": AUTHORIZATION_ID},
    )

    for response in (fetched, created):
        assert response.status_code == 401
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json()["reason_code"] == "AUTH_SESSION_REQUIRED"
    assert service.calls == []


def test_get_returns_the_owned_server_response_without_mutation_dependency(
    make_client: Callable[..., TestClient],
) -> None:
    client, service, current, mutation = client_with_payment_service(
        make_client,
        current_account_id=CURRENT_ACCOUNT_ID,
        mutation_account_id=ACCOUNT_ID,
    )

    response = client.get(f"/api/v1/payment-transactions/{TRANSACTION_ID}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["transaction_id"] == TRANSACTION_ID
    assert response.json()["merchant"] == {"id": MERCHANT_ID, "name": "OrbitIntel"}
    assert service.calls == [("get", TRANSACTION_ID, CURRENT_ACCOUNT_ID)]
    assert current.calls == 1
    assert mutation.calls == 0


def test_mutation_routes_use_the_mutation_principal_and_forward_no_client_owner(
    make_client: Callable[..., TestClient],
) -> None:
    service = StubPaymentService(verify_status=202, reconcile_status=202)
    client, service, current, mutation = client_with_payment_service(
        make_client,
        service,
        current_account_id=CURRENT_ACCOUNT_ID,
        mutation_account_id=ACCOUNT_ID,
    )

    created = client.post(
        "/api/v1/payment-transactions",
        json={"authorization_id": AUTHORIZATION_ID},
    )
    verified = client.post(
        f"/api/v1/payment-transactions/{TRANSACTION_ID}/checkout/verify",
        json=checkout_payload(),
    )
    reconciled = client.post(f"/api/v1/payment-transactions/{TRANSACTION_ID}/reconcile")

    assert (created.status_code, verified.status_code, reconciled.status_code) == (201, 202, 202)
    assert current.calls == 0
    assert mutation.calls == 3
    assert service.calls == [
        ("create", PaymentTransactionCreate(authorization_id=AUTHORIZATION_ID), ACCOUNT_ID),
        (
            "verify",
            TRANSACTION_ID,
            CheckoutVerificationCreate(**checkout_payload()),
            ACCOUNT_ID,
        ),
        ("reconcile", TRANSACTION_ID, ACCOUNT_ID),
    ]


def test_checkout_verification_requires_exactly_the_provider_callback_trio(
    make_client: Callable[..., TestClient],
) -> None:
    client, service, _, _ = client_with_payment_service(make_client)
    endpoint = f"/api/v1/payment-transactions/{TRANSACTION_ID}/checkout/verify"

    valid = client.post(endpoint, json=checkout_payload())

    assert valid.status_code == 200
    assert service.calls == [
        (
            "verify",
            TRANSACTION_ID,
            CheckoutVerificationCreate(**checkout_payload()),
            ACCOUNT_ID,
        )
    ]

    invalid_payloads: list[dict[str, object]] = []
    for missing_field in checkout_payload():
        invalid_payloads.append(
            {key: value for key, value in checkout_payload().items() if key != missing_field}
        )
    invalid_payloads.extend(
        [
            {**checkout_payload(), "amount": 500},
            {**checkout_payload(), "razorpay_payment_id": "not-a-payment-id"},
            {**checkout_payload(), "razorpay_order_id": "not-an-order-id"},
            {**checkout_payload(), "razorpay_signature": "0" * 63},
        ]
    )

    for payload in invalid_payloads:
        assert client.post(endpoint, json=payload).status_code == 422

    assert len(service.calls) == 1


def test_reconcile_forwards_the_path_transaction_and_dynamic_status(
    make_client: Callable[..., TestClient],
) -> None:
    service = StubPaymentService(reconcile_status=202)
    client, service, _, _ = client_with_payment_service(make_client, service)

    response = client.post(f"/api/v1/payment-transactions/{TRANSACTION_ID}/reconcile")

    assert response.status_code == 202
    assert response.json()["transaction_id"] == TRANSACTION_ID
    assert service.calls == [("reconcile", TRANSACTION_ID, ACCOUNT_ID)]


@pytest.mark.parametrize(
    ("error_type", "status_code"),
    [
        (PaymentNotFoundError, 404),
        (PaymentExpiredError, 410),
        (PaymentVerificationError, 400),
        (PaymentConflictError, 409),
        (PaymentIntegrityError, 500),
        (PaymentProviderError, 502),
        (PaymentUnavailableError, 503),
        (PaymentTimeoutError, 504),
    ],
)
def test_payment_failures_have_stable_sanitized_http_mappings(
    make_client: Callable[..., TestClient],
    error_type: type[Exception],
    status_code: int,
) -> None:
    reason_code = f"TEST_{error_type.__name__.upper()}"
    error = error_type("Payment operation failed safely", reason_code)
    client, _, _, _ = client_with_payment_service(
        make_client,
        StubPaymentService(error=error),
    )

    response = client.get(f"/api/v1/payment-transactions/{TRANSACTION_ID}")

    assert response.status_code == status_code
    assert response.json() == {
        "detail": "Payment operation failed safely",
        "reason_code": reason_code,
    }
    assert "Traceback" not in response.text
