from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.providers import (
    CreateProviderOrder,
    PaymentProviderRejectedError,
    PaymentProviderResponseError,
    PaymentProviderTimeoutError,
    PaymentProviderUnavailableError,
    RazorpayPaymentProvider,
)
from app.providers import razorpay as razorpay_adapter
from app.providers.base import validate_provider_order_id, validate_provider_payment_id

NOW = datetime(2026, 8, 25, 12, 34, 56, tzinfo=UTC)
ORDER_ID = "order_ABC123"
PAYMENT_ID = "pay_XYZ789"


def order_payload(
    *,
    order_id: str = ORDER_ID,
    receipt: str = "txn_0001",
) -> dict[str, object]:
    return {
        "id": order_id,
        "entity": "order",
        "amount": 5000,
        "amount_paid": 0,
        "amount_due": 5000,
        "currency": "INR",
        "receipt": receipt,
        "status": "created",
        "partial_payment": False,
        "created_at": int(NOW.timestamp()),
    }


def payment_payload(
    *,
    payment_id: str = PAYMENT_ID,
    order_id: str = ORDER_ID,
) -> dict[str, object]:
    return {
        "id": payment_id,
        "entity": "payment",
        "order_id": order_id,
        "amount": 5000,
        "currency": "INR",
        "status": "captured",
        "captured": True,
        "amount_refunded": 0,
        "method": "upi",
        "created_at": int(NOW.timestamp()),
    }


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeOrderResource:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def create(self, data: dict[str, object], **kwargs: object) -> object:
        self.client.calls.append(("create", data, kwargs))
        return self.client.result("create")

    def fetch(self, order_id: str, data: dict[str, object], **kwargs: object) -> object:
        self.client.calls.append(("fetch_order", order_id, data, kwargs))
        return self.client.result("fetch_order")

    def all(self, data: dict[str, object], **kwargs: object) -> object:
        self.client.calls.append(("all_orders", data, kwargs))
        return self.client.result("all_orders")

    def payments(
        self,
        order_id: str,
        data: dict[str, object],
        **kwargs: object,
    ) -> object:
        self.client.calls.append(("order_payments", order_id, data, kwargs))
        return self.client.result("order_payments")


class FakePaymentResource:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def fetch(self, payment_id: str, data: dict[str, object], **kwargs: object) -> object:
        self.client.calls.append(("fetch_payment", payment_id, data, kwargs))
        return self.client.result("fetch_payment")


class FakeClient:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.calls: list[tuple[Any, ...]] = []
        self.app_details: list[dict[str, str]] = []
        self.retry_settings: list[bool] = []
        self.session = FakeSession()
        self.order = FakeOrderResource(self)
        self.payment = FakePaymentResource(self)

    def set_app_details(self, details: dict[str, str]) -> None:
        self.app_details.append(details)

    def enable_retry(self, enabled: bool) -> None:
        self.retry_settings.append(enabled)

    def result(self, operation: str) -> object:
        result = self.results[operation]
        if isinstance(result, Exception):
            raise result
        return result


class FakeClientFactory:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.clients: list[FakeClient] = []
        self.credentials: list[tuple[str, str]] = []

    def __call__(self, key_id: str, key_secret: str) -> FakeClient:
        self.credentials.append((key_id, key_secret))
        client = FakeClient(self.results)
        self.clients.append(client)
        return client


def provider(factory: FakeClientFactory) -> RazorpayPaymentProvider:
    return RazorpayPaymentProvider(
        key_id="rzp_test_12345678",
        key_secret="super-secret",
        connect_timeout_seconds=1.5,
        read_timeout_seconds=4.5,
        client_factory=factory,
    )


@pytest.mark.asyncio
async def test_create_order_uses_exact_server_material_timeout_and_fresh_session() -> None:
    factory = FakeClientFactory({"create": order_payload()})
    adapter = provider(factory)
    request = CreateProviderOrder(
        amount=5000,
        currency="INR",
        receipt="txn_0001",
        notes={"transaction_id": "txn_0001"},
    )

    first = await adapter.create_order(request)
    second = await adapter.create_order(request)

    assert first == second
    assert len(factory.clients) == 2
    for client in factory.clients:
        operation, data, kwargs = client.calls[0]
        assert operation == "create"
        assert data == {
            "amount": 5000,
            "currency": "INR",
            "receipt": "txn_0001",
            "notes": {"transaction_id": "txn_0001"},
            "partial_payment": False,
        }
        assert kwargs == {"timeout": (1.5, 4.5)}
        assert client.retry_settings == [False]
        assert client.app_details == [{"title": "MeterGate", "version": "0.1.0"}]
        assert client.session.closed

    response_without_partial_flag = order_payload()
    response_without_partial_flag.pop("partial_payment")
    adapter = provider(FakeClientFactory({"create": response_without_partial_flag}))
    assert (await adapter.create_order(request)).status == "created"


@pytest.mark.asyncio
async def test_receipt_lookup_uses_bounded_query_and_exact_filters_results() -> None:
    exact_order = order_payload()
    exact_order["amount_paid"] = None
    factory = FakeClientFactory(
        {
            "all_orders": {
                "entity": "collection",
                "count": 2,
                "items": [exact_order, order_payload(receipt="txn_other")],
            }
        }
    )
    adapter = provider(factory)

    result = await adapter.find_orders_by_receipt(
        "txn_0001",
        created_from=NOW - timedelta(minutes=5),
        created_to=NOW,
    )

    assert [order.receipt for order in result] == ["txn_0001"]
    assert result[0].amount_paid == 0
    assert factory.clients[0].calls == [
        (
            "all_orders",
            {
                "receipt": "txn_0001",
                "count": 100,
                "from": int((NOW - timedelta(minutes=5)).timestamp()),
                "to": int(NOW.timestamp()),
            },
            {"timeout": (1.5, 4.5)},
        )
    ]


@pytest.mark.asyncio
async def test_provider_rejects_malformed_and_cross_order_responses() -> None:
    malformed = order_payload()
    malformed["amount_due"] = 4000
    adapter = provider(FakeClientFactory({"fetch_order": malformed}))
    with pytest.raises(PaymentProviderResponseError, match="invalid order"):
        await adapter.fetch_order(ORDER_ID)

    contradictory_paid = order_payload()
    contradictory_paid["status"] = "paid"
    adapter = provider(FakeClientFactory({"fetch_order": contradictory_paid}))
    with pytest.raises(PaymentProviderResponseError, match="invalid order"):
        await adapter.fetch_order(ORDER_ID)

    null_attempted_balance = order_payload()
    null_attempted_balance.update({"status": "attempted", "amount_paid": None})
    adapter = provider(FakeClientFactory({"fetch_order": null_attempted_balance}))
    with pytest.raises(PaymentProviderResponseError, match="invalid order"):
        await adapter.fetch_order(ORDER_ID)

    contradictory_refund = payment_payload()
    contradictory_refund.update({"status": "refunded", "captured": False, "amount_refunded": 0})
    adapter = provider(FakeClientFactory({"fetch_payment": contradictory_refund}))
    with pytest.raises(PaymentProviderResponseError, match="invalid payment"):
        await adapter.fetch_payment(PAYMENT_ID)

    payments = {
        "entity": "collection",
        "count": 1,
        "items": [payment_payload(order_id="order_BAD")],
    }
    adapter = provider(FakeClientFactory({"order_payments": payments}))
    with pytest.raises(PaymentProviderResponseError, match="different order"):
        await adapter.fetch_payments_for_order(ORDER_ID)

    duplicate_payments = {
        "entity": "collection",
        "count": 2,
        "items": [payment_payload(), payment_payload()],
    }
    adapter = provider(FakeClientFactory({"order_payments": duplicate_payments}))
    with pytest.raises(PaymentProviderResponseError, match="duplicate payments"):
        await adapter.fetch_payments_for_order(ORDER_ID)


@pytest.mark.asyncio
async def test_provider_maps_timeout_and_definite_sdk_rejection_without_details() -> None:
    timeout_adapter = provider(FakeClientFactory({"fetch_order": TimeoutError("secret host")}))
    with pytest.raises(PaymentProviderTimeoutError, match="timed out") as timeout_info:
        await timeout_adapter.fetch_order(ORDER_ID)
    assert "secret host" not in str(timeout_info.value)

    class BadRequestError(Exception):
        pass

    BadRequestError.__module__ = "razorpay.errors"
    rejected_adapter = provider(
        FakeClientFactory({"fetch_order": BadRequestError("secret provider detail")})
    )
    with pytest.raises(PaymentProviderRejectedError, match="rejected") as rejected_info:
        await rejected_adapter.fetch_order(ORDER_ID)
    assert "secret provider detail" not in str(rejected_info.value)


@pytest.mark.asyncio
async def test_rate_limit_status_is_preserved_as_retryable_before_sdk_mapping() -> None:
    class FakeResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.closed = False

        def close(self) -> None:
            self.closed = True

    ordinary_response = FakeResponse(400)
    assert (
        razorpay_adapter._raise_on_rate_limit(ordinary_response)  # noqa: SLF001
        is ordinary_response
    )
    assert not ordinary_response.closed

    rate_limited_response = FakeResponse(429)
    with pytest.raises(RuntimeError, match="rate limited") as rate_limit_info:
        razorpay_adapter._raise_on_rate_limit(rate_limited_response)  # noqa: SLF001
    assert rate_limited_response.closed

    adapter = provider(
        FakeClientFactory({"fetch_order": rate_limit_info.value}),
    )
    with pytest.raises(PaymentProviderUnavailableError, match="later retry"):
        await adapter.fetch_order(ORDER_ID)


def test_provider_ids_fit_persistence_columns_and_require_test_credentials() -> None:
    validate_provider_order_id("order_" + "a" * 58)
    validate_provider_payment_id("pay_" + "a" * 60)
    with pytest.raises(ValueError):
        validate_provider_order_id("order_" + "a" * 59)
    with pytest.raises(ValueError):
        validate_provider_payment_id("pay_" + "a" * 61)
    with pytest.raises(ValueError, match="test-mode"):
        RazorpayPaymentProvider(key_id="rzp_live_12345678", key_secret="super-secret")
