from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.cache.payment_webhooks import VerifiedWebhookPayload
from app.core.config import Settings
from app.domain.enums import (
    CompensationDecisionProvenance,
    CompensationDecisionState,
    CompensationRecommendedAction,
    PaymentProvider,
    PaymentRefundState,
    PaymentTransactionState,
)
from app.domain.exceptions import (
    CompensationIntegrityError,
    CompensationNotFoundError,
    EntitlementConflictError,
    PaymentIntegrityError,
    PaymentVerificationError,
)
from app.domain.refund_webhooks import parse_refund_webhook
from app.models import CompensationCase, CompensationEvent, PaymentRefund
from app.providers import webhook_signature_digest
from app.services.compensations import RefundApplicationService
from app.services.payment_webhooks import RazorpayWebhookIngressService
from tests.test_payment_reconciliation import webhook_payload as payment_webhook_payload
from tests.test_payment_services import (
    ORDER_ID as HARNESS_ORDER_ID,
)
from tests.test_payment_services import (
    PAYMENT_ID as HARNESS_PAYMENT_ID,
)
from tests.test_payment_services import (
    make_harness,
    make_provider_order,
    make_provider_payment,
)

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
WEBHOOK_SECRET = "test-refund-webhook-secret"
EVENT_ID = "evt_refund_delivery_001"
TRANSACTION_ID = "txn_00000000000000000000000001"
PAYMENT_ATTEMPT_ID = "pmt_00000000000000000000000001"
PAYMENT_ID = "pay_RazorpayTestPayment001"
ORDER_ID = "order_RazorpayTestOrder001"
REFUND_ID = "rfd_00000000000000000000000001"
PROVIDER_REFUND_ID = "rfnd_RazorpayTestRefund001"
CASE_ID = "cmp_00000000000000000000000001"


def refund_webhook_body(
    event_type: str,
    *,
    event_created_at: datetime = NOW,
    refund_status: str | None = None,
    amount: int = 500,
    payment_id: str = PAYMENT_ID,
    refund_payment_id: str | None = None,
    order_id: str | None = ORDER_ID,
    receipt: str = REFUND_ID,
    currency: str = "INR",
    payment_currency: str | None = None,
    payment_amount: int = 500,
) -> bytes:
    status = (
        refund_status
        or {
            "refund.created": "processed",
            "refund.processed": "processed",
            "refund.failed": "failed",
            "refund.speed_changed": "processed",
        }[event_type]
    )
    body = {
        "entity": "event",
        "account_id": "acc_RazorpayTestAccount",
        "event": event_type,
        "contains": ["payment", "refund"],
        "created_at": int(event_created_at.timestamp()),
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": payment_amount,
                    "currency": payment_currency or currency,
                    "status": "refunded" if status == "processed" else "captured",
                    "order_id": order_id,
                    "captured": True,
                    "amount_refunded": amount if status == "processed" else 0,
                }
            },
            "refund": {
                "entity": {
                    "id": PROVIDER_REFUND_ID,
                    "entity": "refund",
                    "amount": amount,
                    "currency": currency,
                    "payment_id": refund_payment_id or payment_id,
                    "receipt": receipt,
                    "status": status,
                    "speed_processed": "normal",
                    "speed_requested": "normal",
                    "created_at": int((NOW - timedelta(seconds=5)).timestamp()),
                }
            },
        },
    }
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


class RecordingQueue:
    def __init__(self) -> None:
        self.payloads: list[VerifiedWebhookPayload] = []

    async def enqueue(self, payload: VerifiedWebhookPayload) -> str:
        self.payloads.append(payload)
        return "1-0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    ["refund.created", "refund.processed", "refund.failed", "refund.speed_changed"],
)
async def test_all_supported_refund_webhooks_use_the_raw_body_signature_path(
    event_type: str,
) -> None:
    queue = RecordingQueue()
    ingress = RazorpayWebhookIngressService(
        queue,  # type: ignore[arg-type]
        webhook_secret=WEBHOOK_SECRET,
        maximum_age=timedelta(minutes=5),
        clock=lambda: NOW,
    )
    body = refund_webhook_body(event_type)
    signature = webhook_signature_digest(raw_body=body, webhook_secret=WEBHOOK_SECRET)

    await ingress.accept(
        raw_body=body,
        signature=signature,
        provider_event_id=f"{EVENT_ID}_{event_type}",
    )

    assert [payload.raw_body for payload in queue.payloads] == [body]


@pytest.mark.asyncio
async def test_refund_signature_rejects_a_one_byte_body_change() -> None:
    queue = RecordingQueue()
    ingress = RazorpayWebhookIngressService(
        queue,  # type: ignore[arg-type]
        webhook_secret=WEBHOOK_SECRET,
        maximum_age=timedelta(minutes=5),
        clock=lambda: NOW,
    )
    body = refund_webhook_body("refund.processed")
    changed = body.replace(b'"amount":500', b'"amount":501', 1)
    signature = webhook_signature_digest(raw_body=body, webhook_secret=WEBHOOK_SECRET)

    with pytest.raises(PaymentVerificationError) as error_info:
        await ingress.accept(
            raw_body=changed,
            signature=signature,
            provider_event_id=EVENT_ID,
        )

    assert error_info.value.reason_code == "PAYMENT_WEBHOOK_SIGNATURE_INVALID"
    assert queue.payloads == []


@pytest.mark.parametrize(
    ("event_type", "expected_status"),
    [
        ("refund.created", "processed"),
        ("refund.processed", "processed"),
        ("refund.failed", "failed"),
        ("refund.speed_changed", "processed"),
    ],
)
def test_realistic_refund_payloads_normalize_exact_supported_events(
    event_type: str,
    expected_status: str,
) -> None:
    evidence = parse_refund_webhook(refund_webhook_body(event_type))

    assert evidence.provider_event_type == event_type
    assert evidence.provider_order_id == ORDER_ID
    assert evidence.payment_amount == 500
    assert evidence.payment_amount_refunded == (500 if expected_status == "processed" else 0)
    assert evidence.refund.id == PROVIDER_REFUND_ID
    assert evidence.refund.payment_id == PAYMENT_ID
    assert evidence.refund.amount == 500
    assert evidence.refund.currency == "INR"
    assert evidence.refund.receipt == REFUND_ID
    assert evidence.refund.status == expected_status


def test_refund_webhook_uses_payment_currency_when_refund_currency_is_omitted() -> None:
    evidence = parse_refund_webhook(
        refund_webhook_body("refund.processed", currency="", payment_currency="INR")
    )

    assert evidence.refund.currency == "INR"


@pytest.mark.parametrize(
    "body",
    [
        refund_webhook_body("refund.processed", refund_status="pending"),
        refund_webhook_body("refund.failed", refund_status="processed"),
        refund_webhook_body("refund.created", refund_payment_id="pay_OtherPayment001"),
        refund_webhook_body("refund.created", amount=501),
        refund_webhook_body("refund.created", currency="USD", payment_currency="INR"),
    ],
)
def test_malformed_or_mismatched_refund_bindings_fail_closed(body: bytes) -> None:
    with pytest.raises(ValueError):
        parse_refund_webhook(body)


class MemorySession:
    def __init__(self) -> None:
        self.events: dict[str, CompensationEvent] = {}
        self.commit_count = 0

    async def scalar(self, statement: Any) -> int:
        if "max(compensation_events.sequence)" in str(statement):
            return max((event.sequence for event in self.events.values()), default=0)
        return 0

    def add(self, value: object) -> None:
        if isinstance(value, CompensationEvent):
            self.events[value.idempotency_key] = value

    def add_all(self, values: list[object]) -> None:
        for value in values:
            self.add(value)

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        return None

    async def refresh(self, _value: object) -> None:
        return None


class MemoryEvents:
    def __init__(self, session: MemorySession) -> None:
        self.session = session

    async def get_by_idempotency_key(self, key: str) -> CompensationEvent | None:
        return self.session.events.get(key)


class MemoryRefunds:
    def __init__(self, refund: PaymentRefund) -> None:
        self.refund = refund

    async def get(self, refund_id: str) -> PaymentRefund | None:
        return self.refund if refund_id == self.refund.id else None

    async def get_for_update(self, refund_id: str) -> PaymentRefund | None:
        return await self.get(refund_id)

    async def get_by_provider_refund_id(self, provider_refund_id: str) -> PaymentRefund | None:
        return self.refund if provider_refund_id == self.refund.provider_refund_id else None

    async def get_by_provider_receipt(self, receipt: str) -> PaymentRefund | None:
        return self.refund if receipt == self.refund.provider_receipt else None

    async def list_for_transaction(self, transaction_id: str) -> list[PaymentRefund]:
        return [self.refund] if transaction_id == self.refund.transaction_id else []


class MemoryCases:
    def __init__(self, case: CompensationCase) -> None:
        self.case = case

    async def get_for_update(self, case_id: str) -> CompensationCase | None:
        return self.case if case_id == self.case.id else None


class EmptyAttempts:
    async def get_by_provider_payment_id(self, _payment_id: str) -> None:
        return None


class MemoryTransactions:
    async def get_for_update(self, transaction_id: str) -> object | None:
        if transaction_id != TRANSACTION_ID:
            return None
        return SimpleNamespace(
            id=transaction_id,
            provider_order_id=ORDER_ID,
            amount=500,
        )


def refund_service_harness() -> tuple[
    RefundApplicationService,
    MemorySession,
    CompensationCase,
    PaymentRefund,
]:
    case = CompensationCase(
        id=CASE_ID,
        account_id="acct_00000000000000000000000001",
        transaction_id=TRANSACTION_ID,
        payment_attempt_id=PAYMENT_ATTEMPT_ID,
        entitlement_id="ent_00000000000000000000000001",
        fulfillment_execution_id="ful_00000000000000000000000001",
        quote_id="qte_00000000000000000000000001",
        quote_hash=f"sha256:{'1' * 64}",
        merchant_id="mrc_00000000000000000000000001",
        service_id="svc_00000000000000000000000001",
        amount_paid=500,
        currency="INR",
        failure_code="FULFILLMENT_PERMANENT_FAILURE",
        failure_evidence_version="1",
        failure_evidence_hash=f"sha256:{'2' * 64}",
        recommended_action=CompensationRecommendedAction.FULL_REFUND,
        decision_state=CompensationDecisionState.EXECUTING,
        decision_provenance=CompensationDecisionProvenance.AUTOMATIC_APPROVED,
        approved_refund_amount=500,
        decision_reason_code="COMPENSATION_AUTO_REFUND_APPROVED",
        created_at=NOW - timedelta(minutes=2),
        decided_at=NOW - timedelta(minutes=2),
        closed_at=None,
        revision=2,
    )
    refund = PaymentRefund(
        id=REFUND_ID,
        compensation_case_id=CASE_ID,
        transaction_id=TRANSACTION_ID,
        payment_attempt_id=PAYMENT_ATTEMPT_ID,
        provider=PaymentProvider.RAZORPAY,
        provider_payment_id=PAYMENT_ID,
        provider_refund_id=None,
        amount=500,
        currency="INR",
        refund_state=PaymentRefundState.REFUND_PROCESSING,
        provider_status=None,
        provider_receipt=REFUND_ID,
        created_at=NOW - timedelta(minutes=1),
        requested_at=NOW - timedelta(minutes=1),
        processed_at=None,
        failed_at=None,
        last_reconciled_at=None,
        revision=2,
    )
    session = MemorySession()
    service = RefundApplicationService(
        session,  # type: ignore[arg-type]
        None,
        Settings(
            database_url="postgresql://test:test@localhost:5432/test",
            redis_url="redis://localhost:6379/15",
            _env_file=None,
        ),
        clock=lambda: NOW,
    )
    service._events = MemoryEvents(session)  # type: ignore[assignment]  # noqa: SLF001
    service._refunds = MemoryRefunds(refund)  # type: ignore[assignment]  # noqa: SLF001
    service._cases = MemoryCases(case)  # type: ignore[assignment]  # noqa: SLF001
    service._attempts = EmptyAttempts()  # type: ignore[assignment]  # noqa: SLF001
    service._transactions = MemoryTransactions()  # type: ignore[assignment]  # noqa: SLF001
    return service, session, case, refund


def verified_payload(event_id: str, event_type: str, **overrides: object) -> VerifiedWebhookPayload:
    return VerifiedWebhookPayload(
        provider_event_id=event_id,
        received_at=NOW,
        raw_body=refund_webhook_body(event_type, **overrides),
    )


@pytest.mark.asyncio
async def test_webhook_processing_is_idempotent_and_terminal_state_is_monotonic() -> None:
    service, session, case, refund = refund_service_harness()
    created = verified_payload(
        "evt_refund_created",
        "refund.created",
        refund_status="pending",
    )
    processed = verified_payload("evt_refund_processed", "refund.processed")

    await service.process_refund_webhook(created)
    revision_after_created = refund.revision
    event_count_after_created = len(session.events)
    await service.process_refund_webhook(created)

    assert refund.refund_state is PaymentRefundState.REFUND_PROCESSING
    assert refund.provider_refund_id == PROVIDER_REFUND_ID
    assert refund.revision == revision_after_created
    assert len(session.events) == event_count_after_created

    await service.process_refund_webhook(processed)

    assert refund.refund_state is PaymentRefundState.REFUNDED
    assert refund.provider_status == "processed"
    assert refund.processed_at == NOW
    assert case.decision_state is CompensationDecisionState.COMPLETED
    assert case.closed_at == NOW

    terminal_revision = refund.revision
    terminal_event_count = len(session.events)
    await service.process_refund_webhook(
        verified_payload(
            "evt_refund_stale_pending",
            "refund.speed_changed",
            refund_status="pending",
        )
    )

    assert refund.refund_state is PaymentRefundState.REFUNDED
    assert refund.provider_status == "processed"
    assert refund.revision == terminal_revision
    assert len(session.events) == terminal_event_count


@pytest.mark.asyncio
async def test_failed_webhook_with_zero_cumulative_refund_is_terminal_and_idempotent() -> None:
    service, session, case, refund = refund_service_harness()
    failed = verified_payload("evt_refund_failed", "refund.failed")

    await service.process_refund_webhook(failed)

    assert refund.refund_state is PaymentRefundState.REFUND_FAILED
    assert refund.provider_status == "failed"
    assert refund.failed_at == NOW
    assert case.decision_state is CompensationDecisionState.MANUAL_REVIEW
    revision = refund.revision
    event_count = len(session.events)

    await service.process_refund_webhook(failed)

    assert refund.revision == revision
    assert len(session.events) == event_count


@pytest.mark.asyncio
async def test_webhook_binding_mismatch_routes_reserved_refund_to_reconciliation() -> None:
    service, _session, case, refund = refund_service_harness()

    with pytest.raises(CompensationIntegrityError) as error_info:
        await service.process_refund_webhook(
            verified_payload(
                "evt_refund_wrong_amount",
                "refund.processed",
                amount=400,
            )
        )

    assert error_info.value.reason_code == "REFUND_PROVIDER_RESPONSE_MISMATCH"
    assert refund.refund_state is PaymentRefundState.RECONCILIATION_REQUIRED
    assert case.decision_state is CompensationDecisionState.EXECUTING


@pytest.mark.asyncio
async def test_webhook_payment_total_mismatch_routes_refund_to_reconciliation() -> None:
    service, _session, case, refund = refund_service_harness()

    with pytest.raises(CompensationIntegrityError) as error_info:
        await service.process_refund_webhook(
            verified_payload(
                "evt_refund_wrong_payment_total",
                "refund.processed",
                payment_amount=600,
            )
        )

    assert error_info.value.reason_code == "REFUND_PROVIDER_RESPONSE_MISMATCH"
    assert refund.refund_state is PaymentRefundState.RECONCILIATION_REQUIRED
    assert case.decision_state is CompensationDecisionState.EXECUTING


@pytest.mark.asyncio
async def test_omitted_refund_currency_still_checks_payment_currency_binding() -> None:
    service, _session, case, refund = refund_service_harness()

    with pytest.raises(CompensationIntegrityError) as error_info:
        await service.process_refund_webhook(
            verified_payload(
                "evt_refund_wrong_fallback_currency",
                "refund.processed",
                currency="",
                payment_currency="USD",
            )
        )

    assert error_info.value.reason_code == "REFUND_PROVIDER_RESPONSE_MISMATCH"
    assert refund.refund_state is PaymentRefundState.RECONCILIATION_REQUIRED
    assert case.decision_state is CompensationDecisionState.EXECUTING


@pytest.mark.asyncio
async def test_same_payment_webhook_without_exact_refund_identity_is_not_bound() -> None:
    service, session, _case, refund = refund_service_harness()

    with pytest.raises(CompensationNotFoundError) as error_info:
        await service.process_refund_webhook(
            verified_payload(
                "evt_refund_unrelated_same_payment",
                "refund.processed",
                receipt="rfd_0AAAAAAAAAAAAAAAAAAAAAAAAA",
            )
        )

    assert error_info.value.reason_code == "REFUND_WEBHOOK_UNMATCHED"
    assert refund.provider_refund_id is None
    assert refund.refund_state is PaymentRefundState.REFUND_PROCESSING
    assert session.events == {}


class RecordingRefundRecovery:
    def __init__(self) -> None:
        self.payloads: list[VerifiedWebhookPayload] = []

    async def process_refund_webhook(self, payload: VerifiedWebhookPayload) -> None:
        self.payloads.append(payload)


@pytest.mark.asyncio
async def test_prequeue_quarantine_is_then_processed_and_changed_body_reuse_fails_closed() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    service = harness.build_service()
    await service.process_webhook(
        payment_webhook_payload(
            event_id="evt_capture_before_refund",
            order_id=HARNESS_ORDER_ID,
        )
    )
    recovery = RecordingRefundRecovery()
    service._refund_recovery = recovery  # type: ignore[assignment]  # noqa: SLF001
    payload = VerifiedWebhookPayload(
        provider_event_id="evt_refund_prequeue_then_worker",
        received_at=NOW,
        raw_body=refund_webhook_body(
            "refund.processed",
            order_id=HARNESS_ORDER_ID,
            payment_id=HARNESS_PAYMENT_ID,
            receipt=REFUND_ID,
        ),
    )

    await service.quarantine_value_revoking_webhook(payload)

    stored = harness.state.webhooks_by_provider_event_id[payload.provider_event_id]
    assert stored.processing_reason_code == "PAYMENT_REFUND_EVIDENCE_DETECTED"
    assert recovery.payloads == []

    await service.process_webhook(payload)

    assert recovery.payloads == [payload]
    event_count = len(harness.state.events_by_key)
    changed = VerifiedWebhookPayload(
        provider_event_id=payload.provider_event_id,
        received_at=payload.received_at,
        raw_body=payload.raw_body + b" ",
    )
    with pytest.raises(PaymentIntegrityError) as error_info:
        await service.process_webhook(changed)

    assert error_info.value.reason_code == "PAYMENT_WEBHOOK_EVENT_INTEGRITY_FAILED"
    assert recovery.payloads == [payload]
    assert len(harness.state.events_by_key) == event_count


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id", [None, "order_ContradictoryRefundOrder"])
async def test_signed_refund_uses_known_payment_id_to_quarantine_when_order_is_bad(
    order_id: str | None,
) -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    service = harness.build_service()
    await service.process_webhook(
        payment_webhook_payload(
            event_id=f"evt_capture_before_bad_refund_{order_id or 'missing'}",
            order_id=HARNESS_ORDER_ID,
        )
    )
    payload = VerifiedWebhookPayload(
        provider_event_id=f"evt_refund_bad_order_{order_id or 'missing'}",
        received_at=NOW,
        raw_body=refund_webhook_body(
            "refund.processed",
            order_id=order_id,
            payment_id=HARNESS_PAYMENT_ID,
            receipt=REFUND_ID,
        ),
    )

    await service.quarantine_value_revoking_webhook(payload)

    stored = harness.state.webhooks_by_provider_event_id[payload.provider_event_id]
    assert stored.transaction_id == transaction.id
    expected_reason_code = (
        "PAYMENT_REFUND_EVIDENCE_DETECTED"
        if order_id is None
        else "PAYMENT_REFUND_EVIDENCE_CONFLICT"
    )
    assert stored.processing_reason_code == expected_reason_code
    assert transaction.transaction_state is PaymentTransactionState.PAID
    with pytest.raises(EntitlementConflictError) as error_info:
        await service.require_local_value_release_eligibility(transaction.id)
    assert error_info.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"

    harness.clock.advance()
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)
    with pytest.raises(EntitlementConflictError) as still_blocked:
        await service.require_local_value_release_eligibility(transaction.id)
    assert still_blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payment_id", "refund_payment_id"),
    [
        (HARNESS_PAYMENT_ID, "pay_ContradictoryRefundPayment"),
        ("pay_ContradictoryPaymentEntity", HARNESS_PAYMENT_ID),
    ],
)
async def test_signed_refund_with_contradictory_payment_entities_quarantines_known_payment(
    payment_id: str,
    refund_payment_id: str,
) -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.id,
        status="paid",
    )
    harness.provider.fetch_payments_result = (make_provider_payment(status="captured"),)
    service = harness.build_service()
    await service.process_webhook(
        payment_webhook_payload(
            event_id=f"evt_capture_before_conflicting_refund_{payment_id}",
            order_id=HARNESS_ORDER_ID,
        )
    )
    payload = VerifiedWebhookPayload(
        provider_event_id=f"evt_refund_conflicting_payments_{payment_id}",
        received_at=NOW,
        raw_body=refund_webhook_body(
            "refund.processed",
            order_id=None,
            payment_id=payment_id,
            refund_payment_id=refund_payment_id,
            receipt=REFUND_ID,
        ),
    )

    await service.quarantine_value_revoking_webhook(payload)

    stored = harness.state.webhooks_by_provider_event_id[payload.provider_event_id]
    assert stored.transaction_id == transaction.id
    assert stored.processing_reason_code == "PAYMENT_REFUND_EVIDENCE_CONFLICT"
    assert transaction.transaction_state is PaymentTransactionState.PAID
    anomaly = harness.state.events_by_key[
        f"webhook:{payload.provider_event_id}:refund-integrity:{transaction.id}"
    ]
    assert set(anomaly.event_metadata["provider_payment_ids"]) == {
        payment_id,
        refund_payment_id,
    }
    event_count = len(harness.state.events_by_key)
    await service.process_webhook(payload)
    assert len(harness.state.webhooks_by_provider_event_id) == 2
    assert len(harness.state.events_by_key) == event_count
    with pytest.raises(EntitlementConflictError) as error_info:
        await service.require_local_value_release_eligibility(transaction.id)
    assert error_info.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"

    harness.clock.advance()
    await service.reconcile_transaction(transaction.id, account_id=transaction.account_id)
    with pytest.raises(EntitlementConflictError) as still_blocked:
        await service.require_local_value_release_eligibility(transaction.id)
    assert still_blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"
