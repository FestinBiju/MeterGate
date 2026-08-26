from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from app.providers import (
    CreateProviderRefund,
    PaymentProviderResponseError,
    PaymentProviderTimeoutError,
    PaymentProviderUnavailableError,
    ProviderRefund,
    RazorpayPaymentProvider,
    RefundProvider,
)

NOW = datetime(2026, 8, 26, 12, 34, 56, tzinfo=UTC)
PAYMENT_ID = "pay_XYZ789"
REFUND_ID = "rfnd_REFUND123"
REFUND_RECEIPT = "rfd_0" + "A" * 25


def refund_payload(
    *,
    refund_id: str = REFUND_ID,
    payment_id: str = PAYMENT_ID,
    amount: int = 5000,
    currency: object = "INR",
    receipt: object = REFUND_RECEIPT,
    status: str = "pending",
) -> dict[str, object]:
    return {
        "id": refund_id,
        "entity": "refund",
        "payment_id": payment_id,
        "amount": amount,
        "currency": currency,
        "receipt": receipt,
        "status": status,
        "created_at": int(NOW.timestamp()),
    }


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakePaymentResource:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def refund(
        self,
        payment_id: str,
        data: dict[str, object],
        **kwargs: object,
    ) -> object:
        self.client.calls.append(("refund", payment_id, data, kwargs))
        return self.client.result("refund")

    def fetch_refund_id(
        self,
        payment_id: str,
        refund_id: str,
        **kwargs: object,
    ) -> object:
        self.client.calls.append(("fetch_refund_id", payment_id, refund_id, kwargs))
        return self.client.result("fetch_refund_id")

    def fetch_multiple_refund(
        self,
        payment_id: str,
        data: dict[str, object],
        **kwargs: object,
    ) -> object:
        self.client.calls.append(("fetch_multiple_refund", payment_id, data, kwargs))
        return self.client.result("fetch_multiple_refund")


class FakeClient:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.calls: list[tuple[Any, ...]] = []
        self.app_details: list[dict[str, str]] = []
        self.retry_settings: list[bool] = []
        self.session = FakeSession()
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

    def __call__(self, _key_id: str, _key_secret: str) -> FakeClient:
        client = FakeClient(self.results)
        self.clients.append(client)
        return client


def provider(
    factory: FakeClientFactory,
    *,
    operation_timeout_seconds: float | None = None,
    max_concurrency: int = 8,
) -> RazorpayPaymentProvider:
    return RazorpayPaymentProvider(
        key_id="rzp_test_12345678",
        key_secret="super-secret",
        connect_timeout_seconds=1.5,
        read_timeout_seconds=4.5,
        operation_timeout_seconds=operation_timeout_seconds,
        max_concurrency=max_concurrency,
        client_factory=factory,
    )


def refund_request() -> CreateProviderRefund:
    return CreateProviderRefund(
        payment_id=PAYMENT_ID,
        amount=5000,
        currency="INR",
        receipt=REFUND_RECEIPT,
        notes={"metergate_refund_id": REFUND_RECEIPT},
    )


def test_refund_dtos_and_protocol_are_strict_and_separate() -> None:
    notes = {"metergate_refund_id": REFUND_RECEIPT}
    request = CreateProviderRefund(
        payment_id=PAYMENT_ID,
        amount=5000,
        currency="INR",
        receipt=REFUND_RECEIPT,
        notes=notes,
    )
    notes["late_mutation"] = "not retained"
    assert dict(request.notes) == {"metergate_refund_id": REFUND_RECEIPT}

    normalized = ProviderRefund(
        id=REFUND_ID,
        payment_id=PAYMENT_ID,
        amount=5000,
        currency=None,
        receipt=None,
        status="processed",
        created_at=NOW,
    )
    assert normalized.status == "processed"

    with pytest.raises(ValueError, match="rfd_"):
        CreateProviderRefund(
            payment_id=PAYMENT_ID,
            amount=5000,
            currency="INR",
            receipt="txn_not_a_refund",
            notes={},
        )
    with pytest.raises(ValueError, match="normal"):
        CreateProviderRefund(
            payment_id=PAYMENT_ID,
            amount=5000,
            currency="INR",
            receipt=REFUND_RECEIPT,
            notes={},
            speed="optimum",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="refund ID"):
        ProviderRefund(
            id="refund_wrong_prefix",
            payment_id=PAYMENT_ID,
            amount=5000,
            currency="INR",
            receipt=REFUND_RECEIPT,
            status="pending",
            created_at=NOW,
        )

    adapter = provider(FakeClientFactory({}))
    assert isinstance(adapter, RefundProvider)

    class PaymentOnlyFake:
        async def create_order(self, request: object) -> object:
            return request

        async def fetch_order(self, provider_order_id: str) -> str:
            return provider_order_id

        async def find_orders_by_receipt(self, receipt: str, **kwargs: object) -> tuple[()]:
            del receipt, kwargs
            return ()

        async def fetch_payments_for_order(self, provider_order_id: str) -> tuple[()]:
            del provider_order_id
            return ()

        async def fetch_payment(self, provider_payment_id: str) -> str:
            return provider_payment_id

    assert not isinstance(PaymentOnlyFake(), RefundProvider)


@pytest.mark.asyncio
async def test_create_refund_uses_exact_sdk_call_idempotency_and_fresh_session() -> None:
    factory = FakeClientFactory({"refund": refund_payload()})
    adapter = provider(factory)

    first = await adapter.create_refund(refund_request())
    second = await adapter.create_refund(refund_request())

    assert first == second
    assert first.status == "pending"
    assert len(factory.clients) == 2
    for client in factory.clients:
        assert client.calls == [
            (
                "refund",
                PAYMENT_ID,
                {
                    "amount": 5000,
                    "speed": "normal",
                    "receipt": REFUND_RECEIPT,
                    "notes": {"metergate_refund_id": REFUND_RECEIPT},
                },
                {
                    "headers": {"X-Refund-Idempotency": REFUND_RECEIPT},
                    "timeout": (1.5, 4.5),
                },
            )
        ]
        assert client.retry_settings == [False]
        assert client.app_details == [{"title": "MeterGate", "version": "0.1.0"}]
        assert client.session.closed


@pytest.mark.asyncio
async def test_fetch_and_payment_scoped_list_use_pinned_sdk_methods() -> None:
    second_id = "rfnd_SECOND456"
    factory = FakeClientFactory(
        {
            "fetch_refund_id": refund_payload(status="processed"),
            "fetch_multiple_refund": {
                "entity": "collection",
                "count": 2,
                "items": [
                    refund_payload(status="processed"),
                    refund_payload(
                        refund_id=second_id,
                        currency="",
                        receipt=None,
                        status="failed",
                    ),
                ],
            },
        }
    )
    adapter = provider(factory)

    fetched = await adapter.fetch_refund(PAYMENT_ID, REFUND_ID)
    listed = await adapter.fetch_refunds_for_payment(PAYMENT_ID)

    assert fetched.id == REFUND_ID
    assert [refund.id for refund in listed] == [REFUND_ID, second_id]
    assert listed[1].currency is None
    assert listed[1].receipt is None
    assert factory.clients[0].calls == [
        ("fetch_refund_id", PAYMENT_ID, REFUND_ID, {"timeout": (1.5, 4.5)})
    ]
    assert factory.clients[1].calls == [
        (
            "fetch_multiple_refund",
            PAYMENT_ID,
            {"count": 100},
            {"timeout": (1.5, 4.5)},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("payment_id", "pay_DIFFERENT"),
        ("amount", 4999),
        ("receipt", "rfd_" + "B" * 26),
        ("currency", "USD"),
        ("currency", "inr"),
    ],
)
async def test_create_refund_rejects_response_binding_mismatches(
    changed_field: str,
    changed_value: object,
) -> None:
    payload = refund_payload()
    payload[changed_field] = changed_value
    adapter = provider(FakeClientFactory({"refund": payload}))

    with pytest.raises(PaymentProviderResponseError):
        await adapter.create_refund(refund_request())


@pytest.mark.asyncio
async def test_create_refund_allows_unavailable_currency_but_requires_receipt() -> None:
    unavailable_currency = refund_payload(currency="")
    adapter = provider(FakeClientFactory({"refund": unavailable_currency}))
    assert (await adapter.create_refund(refund_request())).currency is None

    absent_receipt = refund_payload(receipt=None)
    adapter = provider(FakeClientFactory({"refund": absent_receipt}))
    with pytest.raises(PaymentProviderResponseError, match="different payment terms"):
        await adapter.create_refund(refund_request())


@pytest.mark.asyncio
async def test_refund_fetches_reject_duplicates_cross_payment_and_invalid_status() -> None:
    duplicate_collection = {
        "entity": "collection",
        "count": 2,
        "items": [refund_payload(), refund_payload()],
    }
    adapter = provider(FakeClientFactory({"fetch_multiple_refund": duplicate_collection}))
    with pytest.raises(PaymentProviderResponseError, match="duplicate refunds"):
        await adapter.fetch_refunds_for_payment(PAYMENT_ID)

    cross_payment = {
        "entity": "collection",
        "count": 1,
        "items": [refund_payload(payment_id="pay_DIFFERENT")],
    }
    adapter = provider(FakeClientFactory({"fetch_multiple_refund": cross_payment}))
    with pytest.raises(PaymentProviderResponseError, match="different payment"):
        await adapter.fetch_refunds_for_payment(PAYMENT_ID)

    invalid_status = refund_payload(status="completed")
    adapter = provider(FakeClientFactory({"fetch_refund_id": invalid_status}))
    with pytest.raises(PaymentProviderResponseError, match="invalid refund"):
        await adapter.fetch_refund(PAYMENT_ID, REFUND_ID)


@pytest.mark.asyncio
async def test_full_refund_collection_page_fails_closed_as_incomplete() -> None:
    full_page = {
        "entity": "collection",
        "count": 100,
        "items": [refund_payload(refund_id=f"rfnd_PAGE{i:03d}") for i in range(100)],
    }
    adapter = provider(FakeClientFactory({"fetch_multiple_refund": full_page}))

    with pytest.raises(PaymentProviderResponseError, match="may be incomplete"):
        await adapter.fetch_refunds_for_payment(PAYMENT_ID)


@pytest.mark.asyncio
async def test_refund_failures_are_sanitized_and_classified() -> None:
    timeout_adapter = provider(FakeClientFactory({"refund": TimeoutError("secret host")}))
    with pytest.raises(PaymentProviderTimeoutError, match="timed out") as timeout_info:
        await timeout_adapter.create_refund(refund_request())
    assert "secret host" not in str(timeout_info.value)

    class BadRequestError(Exception):
        pass

    BadRequestError.__module__ = "razorpay.errors"
    rejected_adapter = provider(
        FakeClientFactory({"refund": BadRequestError("secret provider detail")})
    )
    with pytest.raises(
        PaymentProviderResponseError, match="requires reconciliation"
    ) as rejected_info:
        await rejected_adapter.create_refund(refund_request())
    assert "secret provider detail" not in str(rejected_info.value)

    unavailable_adapter = provider(
        FakeClientFactory({"refund": ConnectionError("secret endpoint")})
    )
    with pytest.raises(PaymentProviderUnavailableError, match="unavailable") as unavailable_info:
        await unavailable_adapter.create_refund(refund_request())
    assert "secret endpoint" not in str(unavailable_info.value)


@pytest.mark.asyncio
async def test_refund_hard_deadline_retains_capacity_until_sdk_thread_exits() -> None:
    release = threading.Event()

    class BlockingPaymentResource(FakePaymentResource):
        def refund(
            self,
            payment_id: str,
            data: dict[str, object],
            **kwargs: object,
        ) -> object:
            self.client.calls.append(("refund", payment_id, data, kwargs))
            release.wait()
            return refund_payload()

    class BlockingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__({"refund": refund_payload()})
            self.payment = BlockingPaymentResource(self)

    class BlockingFactory:
        def __init__(self) -> None:
            self.clients: list[BlockingClient] = []

        def __call__(self, _key_id: str, _key_secret: str) -> BlockingClient:
            client = BlockingClient()
            self.clients.append(client)
            return client

    factory = BlockingFactory()
    adapter = RazorpayPaymentProvider(
        key_id="rzp_test_12345678",
        key_secret="super-secret",
        connect_timeout_seconds=1,
        read_timeout_seconds=1,
        operation_timeout_seconds=0.02,
        max_concurrency=1,
        client_factory=factory,
    )

    try:
        with pytest.raises(PaymentProviderTimeoutError):
            await adapter.create_refund(refund_request())
        with pytest.raises(PaymentProviderTimeoutError, match="execution slot"):
            await adapter.create_refund(refund_request())
        assert len(factory.clients) == 1
    finally:
        release.set()

    for _ in range(20):
        if not adapter._semaphore.locked():  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    assert not adapter._semaphore.locked()  # noqa: SLF001
    assert (await adapter.create_refund(refund_request())).id == REFUND_ID
