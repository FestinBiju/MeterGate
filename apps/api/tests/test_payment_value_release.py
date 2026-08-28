"""Fail-closed payment evidence tests for the first merchant value release."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.domain.enums import PaymentTransactionEventType, PaymentTransactionState
from app.domain.exceptions import (
    EntitlementConflictError,
    EntitlementUnavailableError,
)
from app.providers import PaymentProviderTimeoutError, ProviderOrder, ProviderPayment
from app.services.payments import PaymentApplicationService
from tests.test_payment_services import PAYMENT_ID as HARNESS_PAYMENT_ID
from tests.test_payment_services import (
    make_harness,
    make_provider_order,
    make_provider_payment,
)

NOW = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)
TRANSACTION_ID = "txn_01M00000000000000000000000"
ORDER_ID = "order_VALUERELEASE1"
OTHER_ORDER_ID = "order_VALUERELEASE2"
PAYMENT_ID = "pay_VALUERELEASE1"
OTHER_PAYMENT_ID = "pay_VALUERELEASE2"


def _transaction(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": TRANSACTION_ID,
        "transaction_state": PaymentTransactionState.PAID,
        "provider_order_id": ORDER_ID,
        "provider_receipt": TRANSACTION_ID,
        "amount": 500,
        "currency": "INR",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _order(
    *,
    order_id: str = ORDER_ID,
    receipt: str = TRANSACTION_ID,
    amount: int = 500,
    currency: str = "INR",
    status: str = "paid",
) -> ProviderOrder:
    amount_paid = amount if status == "paid" else 0
    return ProviderOrder(
        id=order_id,
        amount=amount,
        amount_paid=amount_paid,
        amount_due=amount - amount_paid,
        currency=currency,
        receipt=receipt,
        status=status,  # type: ignore[arg-type]
        created_at=NOW,
    )


def _payment(
    *,
    payment_id: str = PAYMENT_ID,
    order_id: str = ORDER_ID,
    amount: int = 500,
    currency: str = "INR",
    status: str = "captured",
    amount_refunded: int | None = None,
) -> ProviderPayment:
    captured = status in {"captured", "refunded"}
    if amount_refunded is None:
        amount_refunded = amount if status == "refunded" else 0
    return ProviderPayment(
        id=payment_id,
        order_id=order_id,
        amount=amount,
        currency=currency,
        status=status,  # type: ignore[arg-type]
        captured=captured,
        amount_refunded=amount_refunded,
        method="upi",
        created_at=NOW,
    )


def _validate(
    *,
    transaction: Any | None = None,
    order: ProviderOrder | None = None,
    payments: tuple[Any, ...] | None = None,
) -> ProviderPayment | None:
    return PaymentApplicationService._validate_value_release_evidence(  # noqa: SLF001
        transaction or _transaction(),
        order=order or _order(),
        payments=payments if payments is not None else (_payment(),),
    )


def test_exact_fully_paid_order_and_unrefunded_capture_release_value() -> None:
    capture = _payment()

    assert _validate(payments=(capture,)) is capture


@pytest.mark.parametrize("status", ["created", "attempted"])
def test_order_must_be_fully_paid(status: str) -> None:
    assert _validate(order=_order(status=status)) is None


@pytest.mark.parametrize("status", ["created", "authorized", "failed", "refunded"])
def test_payment_must_be_an_unrefunded_capture(status: str) -> None:
    assert _validate(payments=(_payment(status=status),)) is None


def test_partial_refund_on_captured_payment_blocks_value_release() -> None:
    assert _validate(payments=(_payment(amount_refunded=100),)) is None


@pytest.mark.parametrize(
    ("order", "transaction"),
    [
        (_order(order_id=OTHER_ORDER_ID), None),
        (_order(receipt="txn_01M00000000000000000000001"), None),
        (_order(amount=501), None),
        (_order(currency="USD"), None),
        (_order(), _transaction(provider_order_id=None)),
    ],
    ids=["order-id", "receipt", "amount", "currency", "missing-stored-order"],
)
def test_order_binding_mismatch_blocks_value_release(
    order: ProviderOrder,
    transaction: Any | None,
) -> None:
    assert _validate(order=order, transaction=transaction) is None


@pytest.mark.parametrize(
    "payment",
    [
        _payment(order_id=OTHER_ORDER_ID),
        _payment(amount=501),
        _payment(currency="USD"),
    ],
    ids=["order-id", "amount", "currency"],
)
def test_payment_binding_mismatch_blocks_value_release(payment: ProviderPayment) -> None:
    assert _validate(payments=(payment,)) is None


def test_missing_or_duplicate_payment_evidence_blocks_value_release() -> None:
    capture = _payment()

    assert _validate(payments=()) is None
    assert _validate(payments=(capture, capture)) is None


def test_multiple_captures_block_value_release() -> None:
    assert (
        _validate(
            payments=(
                _payment(),
                _payment(payment_id=OTHER_PAYMENT_ID),
            )
        )
        is None
    )


def test_any_refund_evidence_blocks_an_otherwise_valid_capture() -> None:
    assert (
        _validate(
            payments=(
                _payment(),
                _payment(payment_id=OTHER_PAYMENT_ID, status="refunded"),
            )
        )
        is None
    )


def test_prior_failed_attempt_does_not_hide_the_sole_valid_capture() -> None:
    capture = _payment(payment_id=OTHER_PAYMENT_ID)

    assert _validate(payments=(_payment(status="failed"), capture)) is capture


class _Session:
    async def commit(self) -> None:
        raise AssertionError("an ineligible transaction must fail before releasing its lock")


class _TransactionRepository:
    def __init__(self, transaction: SimpleNamespace) -> None:
        self.transaction = transaction

    async def get(self, transaction_id: str) -> SimpleNamespace | None:
        assert transaction_id == TRANSACTION_ID
        return self.transaction


class _Provider:
    async def fetch_order(self, provider_order_id: str) -> ProviderOrder:
        raise AssertionError("provider must not be called for an ineligible transaction")

    async def fetch_payments_for_order(
        self,
        provider_order_id: str,
    ) -> tuple[ProviderPayment, ...]:
        raise AssertionError("provider must not be called for an ineligible transaction")


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        redis_url="redis://localhost:6379/15",
        payments_enabled=True,
        razorpay_key_id="rzp_test_12345678",
        razorpay_key_secret="test-key-secret",
        razorpay_webhook_secret="test-webhook-secret",
        cors_allowed_origins=["http://localhost:3000"],
        webauthn_expected_origins=["http://localhost:3000"],
        _env_file=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        PaymentTransactionState.ORDER_CREATION_PENDING,
        PaymentTransactionState.ORDER_CREATED,
        PaymentTransactionState.ORDER_CREATION_FAILED,
        PaymentTransactionState.ORDER_CREATION_UNCERTAIN,
        PaymentTransactionState.PAYMENT_PENDING,
        PaymentTransactionState.PAYMENT_AUTHORIZED,
        PaymentTransactionState.RECONCILIATION_REQUIRED,
    ],
)
async def test_every_non_paid_transaction_state_is_rejected_before_provider_io(
    state: PaymentTransactionState,
) -> None:
    transaction = _transaction(transaction_state=state)
    service = PaymentApplicationService(
        _Session(),  # type: ignore[arg-type]
        _Provider(),  # type: ignore[arg-type]
        _settings(),
        clock=lambda: NOW,
    )
    service._transactions = _TransactionRepository(transaction)  # type: ignore[assignment]  # noqa: SLF001
    service._verify_transaction_integrity = lambda value: None  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(EntitlementConflictError) as caught:
        await service.verify_transaction_for_value_release(TRANSACTION_ID)

    expected_code = (
        "ENTITLEMENT_RECONCILIATION_REQUIRED"
        if state is PaymentTransactionState.RECONCILIATION_REQUIRED
        else "ENTITLEMENT_TRANSACTION_NOT_PAID"
    )
    assert caught.value.reason_code == expected_code


@pytest.mark.asyncio
async def test_fresh_provider_reverification_returns_a_revision_fenced_proof() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)
    harness.clock.advance()

    proof = await service.verify_transaction_for_value_release(transaction.id)

    assert proof.transaction_id == transaction.id
    assert proof.transaction_revision == transaction.revision
    assert proof.provider_order_id == transaction.provider_order_id
    assert proof.provider_payment_id == HARNESS_PAYMENT_ID
    assert proof.verified_at == harness.clock.current
    proof_events = [
        event
        for event in harness.state.events_by_key.values()
        if event.event_type is PaymentTransactionEventType.PAYMENT_REVERIFIED
    ]
    assert len(proof_events) == 1
    assert proof.payment_reverification_event_id == proof_events[0].id
    assert proof_events[0].transaction_revision == proof.transaction_revision
    assert proof_events[0].event_metadata == {
        "authoritative_snapshot": True,
        "value_release_reverification": True,
        "provider_order_id": proof.provider_order_id,
        "provider_order_status": "paid",
        "provider_payment_id": proof.provider_payment_id,
        "provider_payment_status": "captured",
        "amount_refunded": 0,
    }


@pytest.mark.asyncio
async def test_provider_timeout_cannot_create_a_value_release_proof() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)
    initial_revision = transaction.revision
    harness.provider.fetch_order_outcome = PaymentProviderTimeoutError(
        "fetch_order",
        "temporary timeout",
    )

    with pytest.raises(EntitlementUnavailableError) as caught:
        await service.verify_transaction_for_value_release(transaction.id)

    assert caught.value.reason_code == "ENTITLEMENT_PAYMENT_REVERIFICATION_FAILED"
    assert transaction.revision == initial_revision
    assert all(
        event.event_type is not PaymentTransactionEventType.PAYMENT_REVERIFIED
        for event in harness.state.events_by_key.values()
    )


@pytest.mark.asyncio
async def test_contradictory_fresh_evidence_records_anomaly_and_returns_no_proof() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)
    harness.clock.advance()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        amount=501,
        status="paid",
    )

    with pytest.raises(EntitlementConflictError) as caught:
        await service.verify_transaction_for_value_release(transaction.id)

    assert caught.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"
    # Existing paid evidence stays monotonic, but the append-only anomaly gates value.
    assert transaction.transaction_state is PaymentTransactionState.PAID
    anomaly_events = [
        event
        for event in harness.state.events_by_key.values()
        if event.event_type is PaymentTransactionEventType.RECONCILIATION_REQUIRED
    ]
    assert len(anomaly_events) == 1
    assert anomaly_events[0].reason_code == "PAYMENT_VALUE_RELEASE_EVIDENCE_INVALID"
    assert anomaly_events[0].event_metadata == {"value_release_reverification": True}
    assert all(
        event.event_type is not PaymentTransactionEventType.PAYMENT_REVERIFIED
        for event in harness.state.events_by_key.values()
    )

    # A clean value-release retry cannot silently erase the durable anomaly.
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    with pytest.raises(EntitlementConflictError) as unresolved:
        await service.verify_transaction_for_value_release(transaction.id)
    assert unresolved.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"

    # A separate authoritative reconciliation explicitly closes that exact
    # anomaly revision, after which a fresh value-release verification may run.
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)
    resolution_events = [
        event
        for event in harness.state.events_by_key.values()
        if event.event_metadata.get("resolved_reconciliation_revision")
        == anomaly_events[0].transaction_revision
    ]
    assert len(resolution_events) == 1
    proof = await service.verify_transaction_for_value_release(transaction.id)
    assert proof.provider_order_id == transaction.provider_order_id


@pytest.mark.asyncio
async def test_local_value_release_gate_blocks_anomaly_without_provider_io_until_resolved() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)

    eligible = await service.require_local_value_release_eligibility(transaction.id)
    assert eligible is transaction

    harness.clock.advance()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        amount=501,
        status="paid",
    )
    with pytest.raises(EntitlementConflictError):
        await service.verify_transaction_for_value_release(transaction.id)
    provider_calls = (
        len(harness.provider.fetch_order_calls),
        len(harness.provider.fetch_payments_calls),
    )

    with pytest.raises(EntitlementConflictError) as blocked:
        await service.require_local_value_release_eligibility(transaction.id)

    assert blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"
    assert (
        len(harness.provider.fetch_order_calls),
        len(harness.provider.fetch_payments_calls),
    ) == provider_calls

    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)

    restored = await service.require_local_value_release_eligibility(transaction.id)
    assert restored is transaction


@pytest.mark.asyncio
async def test_authoritative_refund_evidence_cannot_resolve_or_release_a_paid_transaction() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)

    harness.clock.advance()
    harness.provider.fetch_payments_result = (make_provider_payment(status="refunded"),)
    reconciled = await service.reconcile_transaction(
        transaction.id,
        account_id=transaction.account_id,
    )

    assert reconciled.response.state is PaymentTransactionState.PAID
    anomaly = list(harness.state.events_by_key.values())[-1]
    assert anomaly.event_type is PaymentTransactionEventType.RECONCILIATION_REQUIRED
    assert anomaly.reason_code == "PAYMENT_REFUND_EVIDENCE_DETECTED"
    assert anomaly.event_metadata == {"provider_payment_ids": [HARNESS_PAYMENT_ID]}
    with pytest.raises(EntitlementConflictError) as blocked:
        await service.require_local_value_release_eligibility(transaction.id)
    assert blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"

    # Refund evidence is safety-critical and cannot be erased by a later generic
    # clean snapshot; a future explicit operator workflow must resolve it.
    harness.clock.advance()
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)
    with pytest.raises(EntitlementConflictError) as still_blocked:
        await service.require_local_value_release_eligibility(transaction.id)
    assert still_blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
async def test_authoritative_snapshot_missing_known_capture_stays_reconciliation_required() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)

    harness.clock.advance()
    harness.provider.fetch_payments_result = ()
    reconciled = await service.reconcile_transaction(
        transaction.id,
        account_id=transaction.account_id,
    )

    assert reconciled.response.state is PaymentTransactionState.PAID
    anomaly = list(harness.state.events_by_key.values())[-1]
    assert anomaly.event_type is PaymentTransactionEventType.RECONCILIATION_REQUIRED
    assert anomaly.reason_code == "PAYMENT_CAPTURE_EVIDENCE_MISSING"
    assert anomaly.event_metadata == {"provider_payment_ids": [HARNESS_PAYMENT_ID]}
    with pytest.raises(EntitlementConflictError) as blocked:
        await service.require_local_value_release_eligibility(transaction.id)
    assert blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("regression_kind", ["order", "payment"])
async def test_authoritative_paid_evidence_regression_is_an_anomaly(
    regression_kind: str,
) -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)

    harness.clock.advance()
    if regression_kind == "order":
        harness.provider.fetch_order_outcome = make_provider_order(
            receipt=transaction.id,
            status="attempted",
        )
    else:
        harness.provider.fetch_payments_result = (make_provider_payment(status="authorized"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)

    anomaly = list(harness.state.events_by_key.values())[-1]
    assert anomaly.event_type is PaymentTransactionEventType.RECONCILIATION_REQUIRED
    assert anomaly.reason_code == "PAYMENT_PROVIDER_EVIDENCE_REGRESSED"
    assert transaction.transaction_state is PaymentTransactionState.PAID
    with pytest.raises(EntitlementConflictError):
        await service.require_local_value_release_eligibility(transaction.id)


@pytest.mark.asyncio
async def test_multiple_capture_evidence_requires_manual_resolution() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (
        make_provider_payment(status="captured"),
        make_provider_payment(status="captured", payment_id=OTHER_PAYMENT_ID),
    )
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)

    harness.clock.advance()
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)

    assert transaction.transaction_state is PaymentTransactionState.PAID
    with pytest.raises(EntitlementConflictError) as blocked:
        await service.require_local_value_release_eligibility(transaction.id)
    assert blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
async def test_local_capture_identity_contradiction_cannot_be_overwritten_by_a_proof() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)
    harness.clock.advance()
    # A provider payment ID is immutable. Re-observing the same pay_... ID with a
    # different provider creation time is contradictory identity evidence.
    harness.provider.fetch_payments_result = (
        make_provider_payment(
            status="captured",
            created_at=harness.clock.current,
        ),
    )

    with pytest.raises(EntitlementConflictError) as caught:
        await service.verify_transaction_for_value_release(transaction.id)

    assert caught.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"
    assert any(
        event.event_type is PaymentTransactionEventType.RECONCILIATION_REQUIRED
        and event.reason_code == "PAYMENT_PROVIDER_PAYMENT_MISMATCH"
        for event in harness.state.events_by_key.values()
    )
    assert all(
        event.event_type is not PaymentTransactionEventType.PAYMENT_REVERIFIED
        for event in harness.state.events_by_key.values()
    )
