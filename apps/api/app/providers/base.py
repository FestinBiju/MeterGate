"""Small, provider-neutral payment API used by the application service."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from app.domain.hashing import MAX_CANONICAL_INTEGER

ProviderOrderStatus = Literal["created", "attempted", "paid"]
ProviderPaymentStatus = Literal["created", "authorized", "captured", "refunded", "failed"]
ProviderRefundStatus = Literal["pending", "processed", "failed"]

_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_ORDER_ID_PATTERN = re.compile(r"^order_[A-Za-z0-9]{1,58}$")
_PAYMENT_ID_PATTERN = re.compile(r"^pay_[A-Za-z0-9]{1,60}$")
_REFUND_ID_PATTERN = re.compile(r"^rfnd_[A-Za-z0-9]{1,59}$")
_LOCAL_REFUND_RECEIPT_PATTERN = re.compile(r"^rfd_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


class PaymentProviderError(RuntimeError):
    """Sanitized base failure at the external provider boundary."""

    def __init__(self, operation: str, message: str) -> None:
        self.operation = operation
        super().__init__(message)


class PaymentProviderRejectedError(PaymentProviderError):
    """The provider returned a definite request rejection."""


class PaymentProviderUnavailableError(PaymentProviderError):
    """The provider or its transport was unavailable."""


class PaymentProviderTimeoutError(PaymentProviderError):
    """The provider operation exceeded its explicit transport timeout."""


class PaymentProviderResponseError(PaymentProviderError):
    """The provider returned structurally invalid or untrusted data."""


def _validate_positive_amount(value: int, *, field: str = "amount") -> None:
    if type(value) is not int or not 1 <= value <= MAX_CANONICAL_INTEGER:
        raise ValueError(f"Provider {field} must be a positive safe integer")


def _validate_nonnegative_amount(value: int, *, field: str) -> None:
    if type(value) is not int or not 0 <= value <= MAX_CANONICAL_INTEGER:
        raise ValueError(f"Provider {field} must be a non-negative safe integer")


def _validate_currency(value: str) -> None:
    if not isinstance(value, str) or _CURRENCY_PATTERN.fullmatch(value) is None:
        raise ValueError("Provider currency must be a three-letter uppercase code")


def _validate_aware_datetime(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Provider {field} must be timezone-aware")
    return value.astimezone(UTC)


def validate_provider_order_id(value: str) -> None:
    if not isinstance(value, str) or _ORDER_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("Provider order ID is invalid")


def validate_provider_payment_id(value: str) -> None:
    if not isinstance(value, str) or _PAYMENT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("Provider payment ID is invalid")


def validate_provider_refund_id(value: str) -> None:
    if not isinstance(value, str) or _REFUND_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("Provider refund ID is invalid")


def validate_local_refund_receipt(value: str) -> None:
    """Validate the MeterGate refund ID reused as Razorpay's stable receipt/key."""
    if not isinstance(value, str) or _LOCAL_REFUND_RECEIPT_PATTERN.fullmatch(value) is None:
        raise ValueError("Provider refund receipt must be a MeterGate rfd_ identifier")


def validate_provider_receipt(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 40
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("Provider receipt must be a bounded printable value")


def validate_provider_event_type(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 100
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError("Provider event type must be bounded printable ASCII without spaces")


@dataclass(frozen=True, slots=True)
class CreateProviderOrder:
    """Exact, server-derived material for one provider Order creation."""

    amount: int
    currency: str
    receipt: str
    notes: Mapping[str, str]
    partial_payment: Literal[False] = False

    def __post_init__(self) -> None:
        _validate_positive_amount(self.amount)
        _validate_currency(self.currency)
        validate_provider_receipt(self.receipt)
        if self.partial_payment is not False:
            raise ValueError("MeterGate does not permit partial provider payments")
        if not isinstance(self.notes, Mapping) or len(self.notes) > 15:
            raise ValueError("Provider notes may contain at most 15 entries")
        normalized: dict[str, str] = {}
        for key, value in self.notes.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not 1 <= len(key) <= 256
                or len(value) > 256
                or key != key.strip()
                or any(ord(character) < 0x20 for character in key)
                or any(ord(character) < 0x20 for character in value)
            ):
                raise ValueError("Provider notes contain an invalid key or value")
            normalized[key] = value
        object.__setattr__(self, "notes", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class CreateProviderRefund:
    """Exact, server-derived material for one idempotent normal refund."""

    payment_id: str
    amount: int
    currency: str
    receipt: str
    notes: Mapping[str, str]
    speed: Literal["normal"] = "normal"

    def __post_init__(self) -> None:
        validate_provider_payment_id(self.payment_id)
        _validate_positive_amount(self.amount)
        _validate_currency(self.currency)
        validate_local_refund_receipt(self.receipt)
        if self.speed != "normal":
            raise ValueError("MeterGate permits only normal provider refunds")
        if not isinstance(self.notes, Mapping) or len(self.notes) > 15:
            raise ValueError("Provider notes may contain at most 15 entries")
        normalized: dict[str, str] = {}
        for key, value in self.notes.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not 1 <= len(key) <= 256
                or len(value) > 256
                or key != key.strip()
                or any(ord(character) < 0x20 for character in key)
                or any(ord(character) < 0x20 for character in value)
            ):
                raise ValueError("Provider notes contain an invalid key or value")
            normalized[key] = value
        object.__setattr__(self, "notes", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class ProviderOrder:
    """Validated trust-relevant subset of a provider Order."""

    id: str
    amount: int
    amount_paid: int
    amount_due: int
    currency: str
    receipt: str
    status: ProviderOrderStatus
    created_at: datetime

    def __post_init__(self) -> None:
        validate_provider_order_id(self.id)
        _validate_positive_amount(self.amount)
        _validate_nonnegative_amount(self.amount_paid, field="amount_paid")
        _validate_nonnegative_amount(self.amount_due, field="amount_due")
        if self.amount_paid + self.amount_due != self.amount:
            raise ValueError("Provider order paid and due amounts do not sum to its amount")
        _validate_currency(self.currency)
        validate_provider_receipt(self.receipt)
        if self.status not in {"created", "attempted", "paid"}:
            raise ValueError("Provider order status is unsupported")
        if self.status == "paid":
            if self.amount_paid != self.amount or self.amount_due != 0:
                raise ValueError("A paid provider order must be fully paid")
        elif self.amount_paid != 0 or self.amount_due != self.amount:
            raise ValueError("A non-paid provider order cannot contain paid funds")
        object.__setattr__(
            self,
            "created_at",
            _validate_aware_datetime(self.created_at, field="created_at"),
        )


@dataclass(frozen=True, slots=True)
class ProviderPayment:
    """Validated non-instrument subset of one provider payment attempt."""

    id: str
    order_id: str
    amount: int
    currency: str
    status: ProviderPaymentStatus
    captured: bool
    amount_refunded: int
    method: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        validate_provider_payment_id(self.id)
        validate_provider_order_id(self.order_id)
        _validate_positive_amount(self.amount)
        _validate_currency(self.currency)
        if self.status not in {"created", "authorized", "captured", "refunded", "failed"}:
            raise ValueError("Provider payment status is unsupported")
        if type(self.captured) is not bool:
            raise ValueError("Provider captured flag must be boolean")
        if self.status == "captured" and not self.captured:
            raise ValueError("A captured provider payment must set captured=true")
        if self.status == "refunded" and not self.captured:
            raise ValueError("A refunded provider payment must retain captured=true")
        if self.status not in {"captured", "refunded"} and self.captured:
            raise ValueError("A non-captured provider payment cannot set captured=true")
        _validate_nonnegative_amount(self.amount_refunded, field="amount_refunded")
        if self.amount_refunded > self.amount:
            raise ValueError("Provider refunded amount cannot exceed the payment amount")
        if self.status == "refunded" and self.amount_refunded == 0:
            raise ValueError("A refunded provider payment must contain refund evidence")
        if self.method is not None and (
            not isinstance(self.method, str)
            or not 1 <= len(self.method) <= 64
            or self.method != self.method.strip()
        ):
            raise ValueError("Provider payment method is invalid")
        object.__setattr__(
            self,
            "created_at",
            _validate_aware_datetime(self.created_at, field="created_at"),
        )


@dataclass(frozen=True, slots=True)
class ProviderRefund:
    """Validated trust-relevant subset of one provider refund."""

    id: str
    payment_id: str
    amount: int
    currency: str | None
    receipt: str | None
    status: ProviderRefundStatus
    created_at: datetime

    def __post_init__(self) -> None:
        validate_provider_refund_id(self.id)
        validate_provider_payment_id(self.payment_id)
        _validate_positive_amount(self.amount)
        if self.currency is not None:
            _validate_currency(self.currency)
        if self.receipt is not None:
            validate_provider_receipt(self.receipt)
        if self.status not in {"pending", "processed", "failed"}:
            raise ValueError("Provider refund status is unsupported")
        object.__setattr__(
            self,
            "created_at",
            _validate_aware_datetime(self.created_at, field="created_at"),
        )


@runtime_checkable
class PaymentProvider(Protocol):
    """Async provider surface; network SDK details never cross this boundary."""

    async def create_order(self, request: CreateProviderOrder) -> ProviderOrder: ...

    async def fetch_order(self, provider_order_id: str) -> ProviderOrder: ...

    async def find_orders_by_receipt(
        self,
        receipt: str,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> tuple[ProviderOrder, ...]: ...

    async def fetch_payments_for_order(
        self,
        provider_order_id: str,
    ) -> tuple[ProviderPayment, ...]: ...

    async def fetch_payment(self, provider_payment_id: str) -> ProviderPayment: ...


@runtime_checkable
class RefundProvider(Protocol):
    """Refund-only provider surface kept separate from payment-order fakes."""

    async def create_refund(self, request: CreateProviderRefund) -> ProviderRefund: ...

    async def fetch_refund(
        self,
        provider_payment_id: str,
        provider_refund_id: str,
    ) -> ProviderRefund: ...

    async def fetch_refunds_for_payment(
        self,
        provider_payment_id: str,
    ) -> tuple[ProviderRefund, ...]: ...
