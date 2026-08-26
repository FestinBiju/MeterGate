"""Async, strictly normalized adapter around Razorpay's synchronous SDK."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from app.domain.hashing import MAX_CANONICAL_INTEGER
from app.providers.base import (
    CreateProviderOrder,
    CreateProviderRefund,
    PaymentProviderError,
    PaymentProviderRejectedError,
    PaymentProviderResponseError,
    PaymentProviderTimeoutError,
    PaymentProviderUnavailableError,
    ProviderOrder,
    ProviderPayment,
    ProviderRefund,
    validate_provider_order_id,
    validate_provider_payment_id,
    validate_provider_receipt,
    validate_provider_refund_id,
)

_TEST_KEY_ID_PATTERN = re.compile(r"^rzp_test_[A-Za-z0-9]{8,64}$")
_MAX_COLLECTION_ITEMS = 100
_ORDER_STATUSES = {"created", "attempted", "paid"}
_PAYMENT_STATUSES = {"created", "authorized", "captured", "refunded", "failed"}
_REFUND_STATUSES = {"pending", "processed", "failed"}


class _ClosableSession(Protocol):
    def close(self) -> None: ...


class _HTTPResponse(Protocol):
    status_code: int

    def close(self) -> None: ...


class _RazorpayRateLimitError(RuntimeError):
    """Preserve HTTP 429 before the pinned SDK discards its status code."""


def _raise_on_rate_limit(
    response: _HTTPResponse,
    *args: object,
    **kwargs: object,
) -> _HTTPResponse:
    del args, kwargs
    if response.status_code == 429:
        response.close()
        raise _RazorpayRateLimitError("Razorpay request was rate limited")
    return response


class _RazorpayOrderResource(Protocol):
    def create(self, data: dict[str, object], **kwargs: object) -> object: ...

    def fetch(self, order_id: str, data: dict[str, object], **kwargs: object) -> object: ...

    def all(self, data: dict[str, object], **kwargs: object) -> object: ...

    def payments(
        self,
        order_id: str,
        data: dict[str, object],
        **kwargs: object,
    ) -> object: ...


class _RazorpayPaymentResource(Protocol):
    def fetch(
        self,
        payment_id: str,
        data: dict[str, object],
        **kwargs: object,
    ) -> object: ...

    def refund(
        self,
        payment_id: str,
        data: dict[str, object],
        **kwargs: object,
    ) -> object: ...

    def fetch_multiple_refund(
        self,
        payment_id: str,
        data: dict[str, object],
        **kwargs: object,
    ) -> object: ...

    def fetch_refund_id(
        self,
        payment_id: str,
        refund_id: str,
        **kwargs: object,
    ) -> object: ...


class RazorpaySDKClient(Protocol):
    order: _RazorpayOrderResource
    payment: _RazorpayPaymentResource
    session: _ClosableSession


RazorpayClientFactory = Callable[[str, str], RazorpaySDKClient]


def _default_client_factory(key_id: str, key_secret: str) -> RazorpaySDKClient:
    """Build a new SDK client and HTTP session for one operation."""
    requests_module = importlib.import_module("requests")
    razorpay_module = importlib.import_module("razorpay")
    session = requests_module.Session()
    session.hooks.setdefault("response", []).append(_raise_on_rate_limit)
    try:
        return razorpay_module.Client(session=session, auth=(key_id, key_secret))
    except Exception:
        session.close()
        raise


class RazorpayPaymentProvider:
    """Razorpay test-mode provider with bounded synchronous SDK concurrency."""

    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        connect_timeout_seconds: float = 2.0,
        read_timeout_seconds: float = 5.0,
        operation_timeout_seconds: float | None = None,
        max_concurrency: int = 8,
        client_factory: RazorpayClientFactory | None = None,
    ) -> None:
        if not isinstance(key_id, str) or _TEST_KEY_ID_PATTERN.fullmatch(key_id) is None:
            raise ValueError("Razorpay must use a syntactically valid test-mode key ID")
        if (
            not isinstance(key_secret, str)
            or not 8 <= len(key_secret) <= 512
            or key_secret != key_secret.strip()
            or "\x00" in key_secret
        ):
            raise ValueError("Razorpay key secret is invalid")
        self._validate_timeout(connect_timeout_seconds, field="connect timeout", maximum=30)
        self._validate_timeout(read_timeout_seconds, field="read timeout", maximum=60)
        effective_operation_timeout = (
            float(connect_timeout_seconds) + float(read_timeout_seconds) + 1.0
            if operation_timeout_seconds is None
            else operation_timeout_seconds
        )
        self._validate_timeout(
            effective_operation_timeout,
            field="operation timeout",
            maximum=120,
        )
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 32:
            raise ValueError("Razorpay concurrency must be between one and 32")

        self._key_id = key_id
        self._key_secret = key_secret
        self._timeout = (float(connect_timeout_seconds), float(read_timeout_seconds))
        self._operation_timeout = float(effective_operation_timeout)
        self._client_factory = client_factory or _default_client_factory
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def create_order(self, request: CreateProviderOrder) -> ProviderOrder:
        if not isinstance(request, CreateProviderOrder):
            raise ValueError("A validated provider-order request is required")
        data: dict[str, object] = {
            "amount": request.amount,
            "currency": request.currency,
            "receipt": request.receipt,
            "notes": dict(request.notes),
            "partial_payment": False,
        }
        raw = await self._execute(
            "create_order",
            lambda client: client.order.create(data, timeout=self._timeout),
        )
        order = self._normalize_order(raw, operation="create_order")
        if (
            order.amount != request.amount
            or order.currency != request.currency
            or order.receipt != request.receipt
        ):
            raise PaymentProviderResponseError(
                "create_order",
                "Razorpay created an order with different payment terms",
            )
        return order

    async def fetch_order(self, provider_order_id: str) -> ProviderOrder:
        validate_provider_order_id(provider_order_id)
        raw = await self._execute(
            "fetch_order",
            lambda client: client.order.fetch(
                provider_order_id,
                {},
                timeout=self._timeout,
            ),
        )
        order = self._normalize_order(raw, operation="fetch_order")
        if order.id != provider_order_id:
            raise PaymentProviderResponseError(
                "fetch_order",
                "Razorpay returned a different order",
            )
        return order

    async def find_orders_by_receipt(
        self,
        receipt: str,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> tuple[ProviderOrder, ...]:
        validate_provider_receipt(receipt)
        from_timestamp = self._optional_timestamp(created_from, field="created_from")
        to_timestamp = self._optional_timestamp(created_to, field="created_to")
        if (
            from_timestamp is not None
            and to_timestamp is not None
            and from_timestamp > to_timestamp
        ):
            raise ValueError("Provider order search start must not exceed its end")
        data: dict[str, object] = {"receipt": receipt, "count": _MAX_COLLECTION_ITEMS}
        if from_timestamp is not None:
            data["from"] = from_timestamp
        if to_timestamp is not None:
            data["to"] = to_timestamp

        raw = await self._execute(
            "find_orders_by_receipt",
            lambda client: client.order.all(data, timeout=self._timeout),
        )
        items = self._normalize_collection(raw, operation="find_orders_by_receipt")
        orders = tuple(
            self._normalize_order(item, operation="find_orders_by_receipt") for item in items
        )
        return tuple(order for order in orders if order.receipt == receipt)

    async def fetch_payments_for_order(
        self,
        provider_order_id: str,
    ) -> tuple[ProviderPayment, ...]:
        validate_provider_order_id(provider_order_id)
        raw = await self._execute(
            "fetch_payments_for_order",
            lambda client: client.order.payments(
                provider_order_id,
                {},
                timeout=self._timeout,
            ),
        )
        items = self._normalize_collection(raw, operation="fetch_payments_for_order")
        payments = tuple(
            self._normalize_payment(item, operation="fetch_payments_for_order") for item in items
        )
        if len({payment.id for payment in payments}) != len(payments):
            raise PaymentProviderResponseError(
                "fetch_payments_for_order",
                "Razorpay returned duplicate payments for one order",
            )
        if any(payment.order_id != provider_order_id for payment in payments):
            raise PaymentProviderResponseError(
                "fetch_payments_for_order",
                "Razorpay returned a payment bound to a different order",
            )
        return payments

    async def fetch_payment(self, provider_payment_id: str) -> ProviderPayment:
        validate_provider_payment_id(provider_payment_id)
        raw = await self._execute(
            "fetch_payment",
            lambda client: client.payment.fetch(
                provider_payment_id,
                {},
                timeout=self._timeout,
            ),
        )
        payment = self._normalize_payment(raw, operation="fetch_payment")
        if payment.id != provider_payment_id:
            raise PaymentProviderResponseError(
                "fetch_payment",
                "Razorpay returned a different payment",
            )
        return payment

    async def create_refund(self, request: CreateProviderRefund) -> ProviderRefund:
        if not isinstance(request, CreateProviderRefund):
            raise ValueError("A validated provider-refund request is required")
        data: dict[str, object] = {
            "amount": request.amount,
            "speed": "normal",
            "receipt": request.receipt,
            "notes": dict(request.notes),
        }
        raw = await self._execute(
            "create_refund",
            lambda client: client.payment.refund(
                request.payment_id,
                data,
                headers={"X-Refund-Idempotency": request.receipt},
                timeout=self._timeout,
            ),
        )
        refund = self._normalize_refund(raw, operation="create_refund")
        if (
            refund.payment_id != request.payment_id
            or refund.amount != request.amount
            or refund.receipt != request.receipt
            or (refund.currency is not None and refund.currency != request.currency)
        ):
            raise PaymentProviderResponseError(
                "create_refund",
                "Razorpay created a refund with different payment terms",
            )
        return refund

    async def fetch_refund(
        self,
        provider_payment_id: str,
        provider_refund_id: str,
    ) -> ProviderRefund:
        validate_provider_payment_id(provider_payment_id)
        validate_provider_refund_id(provider_refund_id)
        raw = await self._execute(
            "fetch_refund",
            lambda client: client.payment.fetch_refund_id(
                provider_payment_id,
                provider_refund_id,
                timeout=self._timeout,
            ),
        )
        refund = self._normalize_refund(raw, operation="fetch_refund")
        if refund.id != provider_refund_id or refund.payment_id != provider_payment_id:
            raise PaymentProviderResponseError(
                "fetch_refund",
                "Razorpay returned a different refund",
            )
        return refund

    async def fetch_refunds_for_payment(
        self,
        provider_payment_id: str,
    ) -> tuple[ProviderRefund, ...]:
        validate_provider_payment_id(provider_payment_id)
        raw = await self._execute(
            "fetch_refunds_for_payment",
            lambda client: client.payment.fetch_multiple_refund(
                provider_payment_id,
                {"count": _MAX_COLLECTION_ITEMS},
                timeout=self._timeout,
            ),
        )
        items = self._normalize_collection(raw, operation="fetch_refunds_for_payment")
        refunds = tuple(
            self._normalize_refund(item, operation="fetch_refunds_for_payment") for item in items
        )
        if len(refunds) == _MAX_COLLECTION_ITEMS:
            # Razorpay's collection is paginated.  One full page does not prove
            # that the payment has no further refunds, so exact-receipt recovery
            # must fail closed instead of searching an incomplete result set.
            raise PaymentProviderResponseError(
                "fetch_refunds_for_payment",
                "Razorpay refund collection may be incomplete",
            )
        if len({refund.id for refund in refunds}) != len(refunds):
            raise PaymentProviderResponseError(
                "fetch_refunds_for_payment",
                "Razorpay returned duplicate refunds for one payment",
            )
        if any(refund.payment_id != provider_payment_id for refund in refunds):
            raise PaymentProviderResponseError(
                "fetch_refunds_for_payment",
                "Razorpay returned a refund bound to a different payment",
            )
        return refunds

    async def _execute(
        self,
        operation: str,
        callback: Callable[[RazorpaySDKClient], object],
    ) -> object:
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._operation_timeout,
            )
        except TimeoutError as error:
            raise PaymentProviderTimeoutError(
                operation,
                "Razorpay operation timed out before an execution slot was available",
            ) from error

        worker = asyncio.create_task(asyncio.to_thread(self._execute_sync, callback))
        release_immediately = True
        try:
            return await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=self._operation_timeout,
            )
        except TimeoutError as error:
            if not worker.done():
                release_immediately = False
                worker.add_done_callback(self._release_after_abandoned_worker)
            raise PaymentProviderTimeoutError(operation, "Razorpay operation timed out") from error
        except asyncio.CancelledError:
            if not worker.done():
                release_immediately = False
                worker.add_done_callback(self._release_after_abandoned_worker)
            raise
        except PaymentProviderError:
            raise
        except Exception as error:
            raise self._map_error(operation, error) from error
        finally:
            if release_immediately:
                self._semaphore.release()

    def _release_after_abandoned_worker(self, worker: asyncio.Task[object]) -> None:
        """Release capacity only when a timed-out synchronous call actually exits."""
        with contextlib.suppress(BaseException):
            worker.result()
        self._semaphore.release()

    def _execute_sync(self, callback: Callable[[RazorpaySDKClient], object]) -> object:
        client = self._client_factory(self._key_id, self._key_secret)
        try:
            enable_retry = getattr(client, "enable_retry", None)
            if callable(enable_retry):
                enable_retry(False)
            set_app_details = getattr(client, "set_app_details", None)
            if callable(set_app_details):
                set_app_details({"title": "MeterGate", "version": "0.1.0"})
            return callback(client)
        finally:
            session = getattr(client, "session", None)
            close = getattr(session, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()

    @staticmethod
    def _map_error(operation: str, error: Exception) -> PaymentProviderError:
        if isinstance(error, _RazorpayRateLimitError):
            return PaymentProviderUnavailableError(
                operation,
                "Razorpay rate limit requires a later retry",
            )
        error_name = type(error).__name__
        error_module = type(error).__module__
        if error_module.startswith("requests") and error_name == "JSONDecodeError":
            return PaymentProviderResponseError(
                operation,
                "Razorpay returned an invalid response",
            )
        if isinstance(error, TimeoutError) or (
            error_module.startswith("requests")
            and error_name in {"Timeout", "ConnectTimeout", "ReadTimeout"}
        ):
            return PaymentProviderTimeoutError(operation, "Razorpay request timed out")
        if (
            operation == "create_refund"
            and error_module.startswith("razorpay")
            and error_name == "BadRequestError"
        ):
            # The official SDK discards the HTTP status and provider error code
            # when constructing BadRequestError.  That makes a definite 4xx
            # rejection indistinguishable here from duplicate-receipt or
            # concurrent-operation conflicts after a successful side effect.
            # Preserve ambiguity so the application reconciles the stable
            # receipt and never treats this as proof that no refund exists.
            return PaymentProviderResponseError(
                operation,
                "Razorpay refund creation outcome requires reconciliation",
            )
        if error_module.startswith("razorpay") and error_name == "BadRequestError":
            return PaymentProviderRejectedError(operation, "Razorpay rejected the request")
        if error_module.startswith("razorpay") and error_name in {
            "GatewayError",
            "ServerError",
        }:
            return PaymentProviderUnavailableError(
                operation,
                "Razorpay could not complete the request",
            )
        if error_module.startswith("requests") or isinstance(error, (ConnectionError, OSError)):
            return PaymentProviderUnavailableError(operation, "Razorpay is unavailable")
        return PaymentProviderUnavailableError(operation, "Razorpay operation failed")

    @classmethod
    def _normalize_order(cls, raw: object, *, operation: str) -> ProviderOrder:
        try:
            value = cls._mapping(raw)
            cls._expect_literal(value, "entity", "order")
            # Razorpay accepts ``partial_payment=false`` on create but omits the
            # field from some current Order response shapes.  A present value
            # must still be the exact boolean false; omission means the request's
            # disabled setting was not contradicted.
            if value.get("partial_payment", False) is not False:
                raise ValueError("partial payment is not disabled")
            status = cls._string(value, "status")
            if status not in _ORDER_STATUSES:
                raise ValueError("unsupported order status")
            amount = cls._integer(value, "amount", positive=True)
            raw_amount_paid = value.get("amount_paid")
            # The current receipt-filtered Orders collection can return null for
            # amount_paid on a newly-created, wholly-unpaid Order even though a
            # direct fetch returns 0.  Normalize only that non-success case; the
            # ProviderOrder invariant below still rejects all contradictory
            # attempted/paid balances.
            amount_paid = (
                0
                if status == "created" and raw_amount_paid is None
                else cls._integer(value, "amount_paid")
            )
            return ProviderOrder(
                id=cls._string(value, "id"),
                amount=amount,
                amount_paid=amount_paid,
                amount_due=cls._integer(value, "amount_due"),
                currency=cls._string(value, "currency"),
                receipt=cls._string(value, "receipt"),
                status=status,  # type: ignore[arg-type]
                created_at=cls._timestamp(value, "created_at"),
            )
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as error:
            raise PaymentProviderResponseError(
                operation,
                "Razorpay returned an invalid order",
            ) from error

    @classmethod
    def _normalize_payment(cls, raw: object, *, operation: str) -> ProviderPayment:
        try:
            value = cls._mapping(raw)
            cls._expect_literal(value, "entity", "payment")
            status = cls._string(value, "status")
            if status not in _PAYMENT_STATUSES:
                raise ValueError("unsupported payment status")
            method = value.get("method")
            if method is not None and not isinstance(method, str):
                raise TypeError("invalid payment method")
            captured = value["captured"]
            if type(captured) is not bool:
                raise TypeError("invalid captured flag")
            return ProviderPayment(
                id=cls._string(value, "id"),
                order_id=cls._string(value, "order_id"),
                amount=cls._integer(value, "amount", positive=True),
                currency=cls._string(value, "currency"),
                status=status,  # type: ignore[arg-type]
                captured=captured,
                amount_refunded=cls._integer(value, "amount_refunded"),
                method=method,
                created_at=cls._timestamp(value, "created_at"),
            )
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as error:
            raise PaymentProviderResponseError(
                operation,
                "Razorpay returned an invalid payment",
            ) from error

    @classmethod
    def _normalize_refund(cls, raw: object, *, operation: str) -> ProviderRefund:
        try:
            value = cls._mapping(raw)
            cls._expect_literal(value, "entity", "refund")
            status = cls._string(value, "status")
            if status not in _REFUND_STATUSES:
                raise ValueError("unsupported refund status")
            raw_currency = value.get("currency")
            currency = (
                None
                if raw_currency is None or raw_currency == ""
                else cls._string(value, "currency")
            )
            raw_receipt = value.get("receipt")
            receipt = (
                None if raw_receipt is None or raw_receipt == "" else cls._string(value, "receipt")
            )
            return ProviderRefund(
                id=cls._string(value, "id"),
                payment_id=cls._string(value, "payment_id"),
                amount=cls._integer(value, "amount", positive=True),
                currency=currency,
                receipt=receipt,
                status=status,  # type: ignore[arg-type]
                created_at=cls._timestamp(value, "created_at"),
            )
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as error:
            raise PaymentProviderResponseError(
                operation,
                "Razorpay returned an invalid refund",
            ) from error

    @classmethod
    def _normalize_collection(
        cls,
        raw: object,
        *,
        operation: str,
    ) -> tuple[Mapping[str, object], ...]:
        try:
            value = cls._mapping(raw)
            cls._expect_literal(value, "entity", "collection")
            count = cls._integer(value, "count")
            items = value["items"]
            if not isinstance(items, (list, tuple)) or len(items) > _MAX_COLLECTION_ITEMS:
                raise TypeError("invalid collection items")
            if count != len(items):
                raise ValueError("collection count does not match items")
            return tuple(cls._mapping(item) for item in items)
        except (KeyError, TypeError, ValueError) as error:
            raise PaymentProviderResponseError(
                operation,
                "Razorpay returned an invalid collection",
            ) from error

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise TypeError("provider value is not an object")
        if any(not isinstance(key, str) for key in value):
            raise TypeError("provider object key is not a string")
        return value

    @staticmethod
    def _string(value: Mapping[str, object], field: str) -> str:
        result = value[field]
        if not isinstance(result, str):
            raise TypeError(f"provider {field} is not a string")
        return result

    @staticmethod
    def _integer(
        value: Mapping[str, object],
        field: str,
        *,
        positive: bool = False,
    ) -> int:
        result = value[field]
        minimum = 1 if positive else 0
        if type(result) is not int or not minimum <= result <= MAX_CANONICAL_INTEGER:
            raise TypeError(f"provider {field} is not a safe integer")
        return result

    @staticmethod
    def _expect_literal(value: Mapping[str, object], field: str, expected: str) -> None:
        if value[field] != expected:
            raise ValueError(f"provider {field} is invalid")

    @classmethod
    def _timestamp(cls, value: Mapping[str, object], field: str) -> datetime:
        timestamp = cls._integer(value, field)
        return datetime.fromtimestamp(timestamp, tz=UTC)

    @staticmethod
    def _optional_timestamp(value: datetime | None, *, field: str) -> int | None:
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"Provider {field} must be timezone-aware")
        timestamp = int(value.astimezone(UTC).timestamp())
        if not 0 <= timestamp <= MAX_CANONICAL_INTEGER:
            raise ValueError(f"Provider {field} is outside the supported range")
        return timestamp

    @staticmethod
    def _validate_timeout(value: float, *, field: str, maximum: int) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 < value <= maximum
        ):
            raise ValueError(f"Razorpay {field} must be positive and bounded")
