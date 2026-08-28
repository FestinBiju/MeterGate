from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.cache.payment_webhooks import VerifiedWebhookPayload
from app.domain.enums import (
    PaymentAttemptStatus,
    PaymentTransactionEventType,
    PaymentTransactionState,
    WebhookProcessingStatus,
)
from app.domain.exceptions import (
    EntitlementConflictError,
    PaymentIntegrityError,
    PaymentTimeoutError,
)
from app.providers import PaymentProviderTimeoutError, ProviderOrder
from tests.test_payment_services import (
    ACCOUNT_ID,
    NOW,
    ORDER_ID,
    OTHER_ORDER_ID,
    OTHER_PAYMENT_ID,
    PAYMENT_ID,
    ServiceHarness,
    make_harness,
    make_provider_order,
    make_provider_payment,
)


def webhook_payload(
    *,
    event_id: str,
    order_id: str,
    payment_id: str = PAYMENT_ID,
    event_type: str = "payment.captured",
    received_at: datetime = NOW,
) -> VerifiedWebhookPayload:
    event_payload: dict[str, object] = {
        "payment": {
            "entity": {
                "id": payment_id,
                "order_id": order_id,
            }
        }
    }
    if event_type.startswith("refund."):
        event_payload["refund"] = {
            "entity": {
                "id": "rfnd_TESTREFUND01",
                "payment_id": payment_id,
            }
        }
    raw_body = json.dumps(
        {
            "entity": "event",
            "event": event_type,
            "created_at": int(NOW.timestamp()),
            "payload": event_payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return VerifiedWebhookPayload(
        provider_event_id=event_id,
        received_at=received_at,
        raw_body=raw_body,
    )


async def make_uncertain_harness() -> ServiceHarness:
    harness = make_harness()
    harness.provider.create_outcome = PaymentProviderTimeoutError(
        "create_order",
        "ambiguous timeout",
    )
    result = await harness.create_transaction()
    assert result.response.state is PaymentTransactionState.ORDER_CREATION_UNCERTAIN
    return harness


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order_status", "payment_status", "expected_state"),
    [
        ("created", None, PaymentTransactionState.ORDER_CREATED),
        ("attempted", "authorized", PaymentTransactionState.PAYMENT_AUTHORIZED),
        ("paid", "captured", PaymentTransactionState.PAID),
    ],
)
async def test_ambiguous_order_creation_recovers_once_by_stable_receipt(
    order_status: str,
    payment_status: str | None,
    expected_state: PaymentTransactionState,
) -> None:
    harness = await make_uncertain_harness()
    transaction = harness.transaction
    recovered = make_provider_order(
        receipt=transaction.provider_receipt,
        status=order_status,
    )
    harness.provider.find_result = (recovered,)
    harness.provider.fetch_order_outcome = recovered
    harness.provider.fetch_payments_result = (
        (make_provider_payment(status=payment_status),) if payment_status is not None else ()
    )

    result = await harness.create_transaction()

    assert result.response.state is expected_state
    assert result.response.provider_order_id == ORDER_ID
    assert len(harness.provider.create_calls) == 1
    assert harness.provider.find_calls[0][0] == transaction.id
    if payment_status is None:
        assert harness.provider.fetch_order_calls == []
    else:
        assert harness.provider.fetch_order_calls == [ORDER_ID]


@pytest.mark.asyncio
async def test_receipt_recovery_with_conflicting_terms_or_multiple_orders_fails_closed() -> None:
    mismatch = await make_uncertain_harness()
    mismatch.provider.find_result = (
        make_provider_order(receipt=mismatch.transaction.id, amount=501),
    )

    mismatched_result = await mismatch.create_transaction()

    assert mismatched_result.response.state is PaymentTransactionState.RECONCILIATION_REQUIRED
    assert mismatch.transaction.provider_order_id is None

    duplicate = await make_uncertain_harness()
    duplicate.provider.find_result = (
        make_provider_order(receipt=duplicate.transaction.id),
        make_provider_order(
            receipt=duplicate.transaction.id,
            order_id=OTHER_ORDER_ID,
        ),
    )

    duplicate_result = await duplicate.create_transaction()

    assert duplicate_result.response.state is PaymentTransactionState.RECONCILIATION_REQUIRED
    assert duplicate.transaction.provider_order_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("order_status", ["attempted", "paid"])
async def test_advanced_receipt_recovery_stays_fail_closed_when_followup_fetch_times_out(
    order_status: str,
) -> None:
    harness = await make_uncertain_harness()
    transaction = harness.transaction
    recovered = make_provider_order(receipt=transaction.id, status=order_status)
    harness.provider.find_result = (recovered,)
    harness.provider.fetch_order_outcome = PaymentProviderTimeoutError(
        "fetch_order",
        "transient timeout",
    )

    with pytest.raises(PaymentTimeoutError):
        await harness.create_transaction()

    response = await harness.build_service().get_transaction(
        transaction.id,
        account_id=ACCOUNT_ID,
    )
    assert response.state is PaymentTransactionState.RECONCILIATION_REQUIRED
    assert response.provider_order_status == order_status
    assert response.checkout is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        "order_amount",
        "order_currency",
        "order_receipt",
        "payment_currency",
        "duplicate_payment_ids",
    ],
)
async def test_reconciliation_rejects_provider_binding_mismatch(mismatch: str) -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    order = make_provider_order(receipt=transaction.id)
    payment = make_provider_payment(status="captured")
    if mismatch == "order_amount":
        order = make_provider_order(receipt=transaction.id, amount=501)
    elif mismatch == "order_currency":
        order = make_provider_order(receipt=transaction.id, currency="USD")
    elif mismatch == "order_receipt":
        order = make_provider_order(receipt="txn_00000000000000000000000099")
    elif mismatch == "payment_currency":
        payment = make_provider_payment(status="captured", currency="USD")
    harness.provider.fetch_order_outcome = order
    harness.provider.fetch_payments_result = (
        (payment, payment) if mismatch == "duplicate_payment_ids" else (payment,)
    )
    harness.clock.advance()

    result = await harness.build_service().reconcile_transaction(
        transaction.id,
        account_id=ACCOUNT_ID,
    )

    assert result.response.state is PaymentTransactionState.RECONCILIATION_REQUIRED
    assert harness.state.attempts_by_payment_id == {}


@pytest.mark.asyncio
async def test_failed_attempt_then_distinct_captured_attempt_preserves_both_records() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    failed = make_provider_payment(status="failed", payment_id=PAYMENT_ID)
    captured = make_provider_payment(status="captured", payment_id=OTHER_PAYMENT_ID)

    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="attempted",
    )
    harness.provider.fetch_payments_result = (failed,)
    await service.process_webhook(
        webhook_payload(
            event_id="opaque-event-failed-first",
            order_id=ORDER_ID,
            payment_id=PAYMENT_ID,
            event_type="payment.failed",
        )
    )

    assert transaction.transaction_state is PaymentTransactionState.PAYMENT_PENDING
    assert harness.state.attempts_by_payment_id[PAYMENT_ID].provider_status is (
        PaymentAttemptStatus.FAILED
    )

    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (failed, captured)
    await service.process_webhook(
        webhook_payload(
            event_id="opaque-event-captured-second",
            order_id=ORDER_ID,
            payment_id=OTHER_PAYMENT_ID,
        )
    )

    assert transaction.transaction_state is PaymentTransactionState.PAID
    assert set(harness.state.attempts_by_payment_id) == {PAYMENT_ID, OTHER_PAYMENT_ID}
    assert harness.state.attempts_by_payment_id[PAYMENT_ID].provider_status is (
        PaymentAttemptStatus.FAILED
    )
    assert harness.state.attempts_by_payment_id[OTHER_PAYMENT_ID].provider_status is (
        PaymentAttemptStatus.CAPTURED
    )


@pytest.mark.asyncio
async def test_captured_webhook_then_late_authorized_webhook_cannot_regress() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)

    await service.process_webhook(
        webhook_payload(event_id="opaque-event-captured", order_id=ORDER_ID)
    )
    paid_at = transaction.paid_at

    harness.clock.advance()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="attempted",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="authorized"),)
    await service.process_webhook(
        webhook_payload(
            event_id="opaque-event-authorized-late",
            order_id=ORDER_ID,
            event_type="payment.authorized",
        )
    )

    assert transaction.transaction_state is PaymentTransactionState.PAID
    assert transaction.provider_order_status == "paid"
    assert transaction.paid_at == paid_at
    assert harness.state.attempts_by_payment_id[PAYMENT_ID].provider_status is (
        PaymentAttemptStatus.CAPTURED
    )


@pytest.mark.asyncio
async def test_refund_webhook_triggers_authoritative_anomaly_gate() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.process_webhook(
        webhook_payload(event_id="opaque-event-captured-before-refund", order_id=ORDER_ID)
    )
    provider_calls = (
        len(harness.provider.fetch_order_calls),
        len(harness.provider.fetch_payments_calls),
    )

    harness.clock.advance()
    # A signed refund event must block value release even while the provider's
    # list endpoint still lags with its prior captured snapshot.
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    refund = webhook_payload(
        event_id="opaque-event-refund-processed",
        order_id=ORDER_ID,
        event_type="refund.processed",
    )
    await service.process_webhook(refund)

    assert transaction.transaction_state is PaymentTransactionState.PAID
    stored = harness.state.webhooks_by_provider_event_id[refund.provider_event_id]
    assert stored.processing_status is WebhookProcessingStatus.RECONCILIATION_REQUIRED
    assert stored.processing_reason_code == "PAYMENT_REFUND_EVIDENCE_DETECTED"
    anomaly = harness.state.events_by_key["webhook:opaque-event-refund-processed"]
    assert anomaly.event_type is PaymentTransactionEventType.RECONCILIATION_REQUIRED
    assert anomaly.reason_code == "PAYMENT_REFUND_EVIDENCE_DETECTED"
    assert (
        len(harness.provider.fetch_order_calls),
        len(harness.provider.fetch_payments_calls),
    ) == provider_calls


@pytest.mark.asyncio
async def test_refund_admission_quarantine_blocks_value_before_queue_worker() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    await service.process_webhook(
        webhook_payload(event_id="opaque-event-captured-before-admission", order_id=ORDER_ID)
    )
    provider_calls = (
        len(harness.provider.fetch_order_calls),
        len(harness.provider.fetch_payments_calls),
    )
    refund = webhook_payload(
        event_id="opaque-event-refund-admission",
        order_id=ORDER_ID,
        event_type="refund.processed",
    )

    await service.quarantine_value_revoking_webhook(refund)

    with pytest.raises(EntitlementConflictError) as blocked:
        await service.require_local_value_release_eligibility(transaction.id)
    assert blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"
    assert (
        len(harness.provider.fetch_order_calls),
        len(harness.provider.fetch_payments_calls),
    ) == provider_calls


@pytest.mark.asyncio
async def test_unattached_order_refund_is_left_for_worker_receipt_recovery() -> None:
    harness = await make_uncertain_harness()
    transaction = harness.transaction
    service = harness.build_service()
    refund = webhook_payload(
        event_id="opaque-event-refund-before-order-attachment",
        order_id=ORDER_ID,
        event_type="refund.processed",
    )

    await service.quarantine_value_revoking_webhook(refund)

    assert transaction.provider_order_id is None
    assert refund.provider_event_id not in harness.state.webhooks_by_provider_event_id
    assert harness.provider.fetch_order_calls == []
    assert harness.provider.fetch_payments_calls == []

    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)

    worker_service = harness.build_service()
    await worker_service.process_webhook(refund)

    assert transaction.provider_order_id == ORDER_ID
    assert transaction.transaction_state is PaymentTransactionState.RECONCILIATION_REQUIRED
    stored = harness.state.webhooks_by_provider_event_id[refund.provider_event_id]
    assert stored.transaction_id == transaction.id
    assert stored.processing_status is WebhookProcessingStatus.RECONCILIATION_REQUIRED
    assert stored.processing_reason_code == "PAYMENT_REFUND_EVIDENCE_DETECTED"
    refund_events = [
        event
        for event in harness.state.events_by_key.values()
        if event.reason_code == "PAYMENT_REFUND_EVIDENCE_DETECTED"
    ]
    assert len(refund_events) == 1
    provider_calls = (
        len(harness.provider.fetch_order_calls),
        len(harness.provider.fetch_payments_calls),
    )
    event_count = len(harness.state.events_by_key)

    await worker_service.process_webhook(refund)

    assert (
        len(harness.provider.fetch_order_calls),
        len(harness.provider.fetch_payments_calls),
    ) == provider_calls
    assert len(harness.state.events_by_key) == event_count


@pytest.mark.asyncio
async def test_refund_recovery_fences_concurrent_paid_attachment_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await make_uncertain_harness()
    transaction = harness.transaction
    recovered_order = make_provider_order(receipt=transaction.id, status="paid")
    captured_payment = make_provider_payment(status="captured")
    harness.provider.fetch_order_outcome = recovered_order
    harness.provider.fetch_payments_result = (captured_payment,)
    refund = webhook_payload(
        event_id="opaque-event-refund-concurrent-attachment",
        order_id=ORDER_ID,
        event_type="refund.processed",
    )
    worker_service = harness.build_service()
    competing_service = harness.build_service()
    original_worker_attach = worker_service._attach_provider_order  # noqa: SLF001
    provider_calls_after_paid: tuple[int, int] | None = None

    async def attach_after_competing_paid_transition(
        transaction_id: str,
        order: ProviderOrder,
    ) -> object:
        nonlocal provider_calls_after_paid
        await competing_service._attach_provider_order(  # noqa: SLF001
            transaction_id,
            order,
        )
        await competing_service.process_webhook(
            webhook_payload(
                event_id="opaque-event-concurrent-captured",
                order_id=ORDER_ID,
            )
        )
        assert transaction.transaction_state is PaymentTransactionState.PAID
        provider_calls_after_paid = (
            len(harness.provider.fetch_order_calls),
            len(harness.provider.fetch_payments_calls),
        )
        return await original_worker_attach(
            transaction_id,
            order,
        )

    monkeypatch.setattr(
        worker_service,
        "_attach_provider_order",
        attach_after_competing_paid_transition,
    )

    await worker_service.process_webhook(refund)

    assert provider_calls_after_paid == (2, 1)
    assert (
        len(harness.provider.fetch_order_calls),
        len(harness.provider.fetch_payments_calls),
    ) == provider_calls_after_paid
    assert transaction.transaction_state is PaymentTransactionState.PAID
    with pytest.raises(EntitlementConflictError) as blocked:
        await harness.build_service().require_local_value_release_eligibility(transaction.id)
    assert blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"
    stored = harness.state.webhooks_by_provider_event_id[refund.provider_event_id]
    assert stored.transaction_id == transaction.id
    assert stored.processing_reason_code == "PAYMENT_REFUND_EVIDENCE_DETECTED"


@pytest.mark.asyncio
async def test_captured_and_order_paid_deliveries_emit_one_success_audit() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)

    await service.process_webhook(
        webhook_payload(event_id="opaque-event-payment-captured", order_id=ORDER_ID)
    )
    await service.process_webhook(
        webhook_payload(
            event_id="opaque-event-order-paid",
            order_id=ORDER_ID,
            event_type="order.paid",
        )
    )

    events = tuple(harness.state.events_by_key.values())
    success_types = {
        PaymentTransactionEventType.PAYMENT_CAPTURED,
        PaymentTransactionEventType.ORDER_PAID,
    }
    assert sum(event.event_type in success_types for event in events) == 1
    assert (
        harness.state.events_by_key["webhook:opaque-event-order-paid"].event_type
        is PaymentTransactionEventType.PAYMENT_RECONCILED
    )
    assert transaction.transaction_state is PaymentTransactionState.PAID


@pytest.mark.asyncio
async def test_backward_clock_keeps_reconciliation_and_webhook_timestamps_monotonic() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    received_at = NOW + timedelta(seconds=20)
    harness.clock.current = NOW - timedelta(minutes=1)
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    payload = webhook_payload(
        event_id="opaque-event-backward-clock",
        order_id=ORDER_ID,
        received_at=received_at,
    )

    await harness.build_service().process_webhook(payload)

    stored = harness.state.webhooks_by_provider_event_id[payload.provider_event_id]
    assert stored.processed_at == received_at
    assert stored.processed_at >= stored.received_at
    assert transaction.last_reconciled_at >= transaction.order_creation_started_at


@pytest.mark.asyncio
async def test_provider_evidence_is_monotonic_across_out_of_order_reconciliation() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()

    async def reconcile(payment_status: str, order_status: str) -> None:
        harness.clock.advance()
        harness.provider.fetch_order_outcome = make_provider_order(
            receipt=transaction.id,
            status=order_status,
        )
        harness.provider.fetch_payments_result = (make_provider_payment(status=payment_status),)
        await service.reconcile_transaction(transaction.id, account_id=ACCOUNT_ID)

    await reconcile("authorized", "attempted")
    assert transaction.transaction_state is PaymentTransactionState.PAYMENT_AUTHORIZED
    assert harness.state.attempts_by_payment_id[PAYMENT_ID].provider_status is (
        PaymentAttemptStatus.AUTHORIZED
    )

    await reconcile("failed", "attempted")
    assert transaction.transaction_state is PaymentTransactionState.PAYMENT_AUTHORIZED
    assert harness.state.attempts_by_payment_id[PAYMENT_ID].provider_status is (
        PaymentAttemptStatus.AUTHORIZED
    )

    await reconcile("captured", "paid")
    captured_paid_at = transaction.paid_at
    assert transaction.transaction_state is PaymentTransactionState.PAID
    assert harness.state.attempts_by_payment_id[PAYMENT_ID].captured

    await reconcile("authorized", "attempted")
    assert transaction.transaction_state is PaymentTransactionState.PAID
    assert transaction.paid_at == captured_paid_at
    assert harness.state.attempts_by_payment_id[PAYMENT_ID].provider_status is (
        PaymentAttemptStatus.CAPTURED
    )


@pytest.mark.asyncio
async def test_duplicate_webhook_is_idempotent_and_body_reuse_conflict_fails_closed() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    payload = webhook_payload(
        event_id="opaque-event-1",
        order_id=transaction.provider_order_id,
    )
    service = harness.build_service()

    await service.process_webhook(payload)
    provider_call_count = len(harness.provider.fetch_order_calls)
    event_count = len(harness.state.events_by_key)
    await service.process_webhook(payload)

    assert transaction.transaction_state is PaymentTransactionState.PAID
    assert len(harness.provider.fetch_order_calls) == provider_call_count
    assert len(harness.state.events_by_key) == event_count
    stored = harness.state.webhooks_by_provider_event_id[payload.provider_event_id]
    assert stored.processing_status is WebhookProcessingStatus.PROCESSED

    conflicting = VerifiedWebhookPayload(
        provider_event_id=payload.provider_event_id,
        received_at=payload.received_at,
        raw_body=payload.raw_body + b" ",
    )
    with pytest.raises(PaymentIntegrityError) as error_info:
        await service.process_webhook(conflicting)
    assert error_info.value.reason_code == "PAYMENT_WEBHOOK_EVENT_INTEGRITY_FAILED"


@pytest.mark.asyncio
async def test_out_of_order_webhook_recovers_pending_transaction_by_provider_receipt() -> None:
    harness = await make_uncertain_harness()
    transaction = harness.transaction
    recovered_order = make_provider_order(receipt=transaction.id, status="paid")
    harness.provider.fetch_order_outcome = recovered_order
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    payload = webhook_payload(event_id="opaque-event-race", order_id=ORDER_ID)

    await harness.build_service().process_webhook(payload)

    assert transaction.provider_order_id == ORDER_ID
    assert transaction.transaction_state is PaymentTransactionState.PAID
    stored = harness.state.webhooks_by_provider_event_id[payload.provider_event_id]
    assert stored.processing_status is WebhookProcessingStatus.PROCESSED
    assert stored.transaction_id == transaction.id
    assert harness.provider.fetch_order_calls == [ORDER_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("order_status", ["attempted", "paid"])
async def test_out_of_order_webhook_recovery_suppresses_checkout_on_transient_followup_failure(
    order_status: str,
) -> None:
    harness = await make_uncertain_harness()
    transaction = harness.transaction
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status=order_status,
    )
    harness.provider.fetch_payments_result = PaymentProviderTimeoutError(
        "fetch_payments_for_order",
        "transient timeout",
    )
    payload = webhook_payload(
        event_id=f"opaque-event-{order_status}",
        order_id=ORDER_ID,
        event_type="payment.authorized",
    )

    with pytest.raises(PaymentProviderTimeoutError):
        await harness.build_service().process_webhook(payload)

    response = await harness.build_service().get_transaction(
        transaction.id,
        account_id=ACCOUNT_ID,
    )
    assert response.state is PaymentTransactionState.RECONCILIATION_REQUIRED
    assert response.provider_order_status == order_status
    assert response.checkout is None
    assert payload.provider_event_id not in harness.state.webhooks_by_provider_event_id


@pytest.mark.asyncio
async def test_truly_unknown_provider_order_is_persisted_without_payment_mutation() -> None:
    harness = make_harness()
    unknown_receipt = "txn_00000000000000000000000099"
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=unknown_receipt,
        order_id=OTHER_ORDER_ID,
        status="attempted",
    )
    payload = webhook_payload(
        event_id="opaque-event-unknown",
        order_id=OTHER_ORDER_ID,
        event_type="payment.failed",
    )

    await harness.build_service().process_webhook(payload)

    assert harness.state.transactions == {}
    assert harness.state.attempts_by_payment_id == {}
    assert harness.provider.fetch_payments_calls == []
    stored = harness.state.webhooks_by_provider_event_id[payload.provider_event_id]
    assert stored.processing_status is WebhookProcessingStatus.IGNORED
    assert stored.processing_reason_code == "PAYMENT_WEBHOOK_ORDER_UNMATCHED"
