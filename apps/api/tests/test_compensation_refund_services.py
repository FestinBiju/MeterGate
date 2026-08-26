"""PostgreSQL integration coverage for compensation and refund recovery."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from app.application import create_app
from app.cache.payment_webhooks import VerifiedWebhookPayload
from app.core.config import Settings
from app.domain.enums import (
    CompensationDecisionProvenance,
    CompensationDecisionState,
    CompensationEventActorType,
    CompensationEventType,
    CompensationRecommendedAction,
    FulfillmentExecutionState,
    PaymentProvider,
    PaymentRefundState,
    WebhookProcessingStatus,
)
from app.domain.exceptions import (
    CompensationIntegrityError,
    CompensationTimeoutError,
    CompensationUnavailableError,
    EntitlementConflictError,
)
from app.domain.hashing import sha256_bytes
from app.domain.ids import (
    new_compensation_event_id,
    new_fulfillment_execution_id,
    new_payment_refund_id,
    new_razorpay_webhook_event_id,
)
from app.models import (
    CompensationCase,
    CompensationEvent,
    Entitlement,
    FulfillmentExecution,
    PaymentRefund,
    PaymentTransaction,
    PaymentTransactionEvent,
    RazorpayWebhookEvent,
    RefundOutboxEvent,
)
from app.providers import (
    CreateProviderRefund,
    PaymentProviderRejectedError,
    PaymentProviderTimeoutError,
    ProviderRefund,
)
from app.repositories import (
    CompensationCaseRepository,
    PaymentRefundRepository,
    RefundOutboxEventRepository,
)
from app.services.compensations import CompensationApplicationService, RefundApplicationService
from app.services.payments import PaymentApplicationService
from app.services.readiness import ReadinessService
from app.workers.entitlements import EntitlementOutboxWorker
from app.workers.refunds import RefundOutboxWorker
from tests.test_entitlement_worker import (
    ExactPaymentProvider,
    _create_purchase_evidence,
    _defer_existing_work,
    _persist_payment,
    _worker_settings,
)
from tests.test_postgresql_domain import (
    IsolatedDatabase,
    _drop_isolated_schema,
    _prepare_isolated_database,
    healthy_probe,
)

NOW = datetime(2026, 8, 26, 12, 34, 56, tzinfo=UTC)
API_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class ApprovedCaseFixture:
    case_id: str
    transaction_id: str
    payment_attempt_id: str
    provider_order_id: str
    provider_payment_id: str
    amount: int
    currency: str
    outbox_available_at: datetime


class ScriptedRefundProvider:
    """Deterministic fake with provider-side receipt persistence."""

    def __init__(
        self,
        *,
        create_status: Literal["pending", "processed", "failed"] = "pending",
        create_failure: Literal["timeout_after_create", "rejected", "mismatch"] | None = None,
    ) -> None:
        self.create_status = create_status
        self.create_failure = create_failure
        self.remote_refunds: dict[str, ProviderRefund] = {}
        self.create_requests: list[CreateProviderRefund] = []
        self.list_calls: list[str] = []
        self.fetch_calls: list[tuple[str, str]] = []

    async def create_refund(self, request: CreateProviderRefund) -> ProviderRefund:
        self.create_requests.append(request)
        evidence = self._evidence(request, status=self.create_status)
        if self.create_failure == "rejected":
            raise PaymentProviderRejectedError("create_refund", "request rejected")
        if self.create_failure == "mismatch":
            return ProviderRefund(
                id=evidence.id,
                payment_id=evidence.payment_id,
                amount=evidence.amount + 1,
                currency=evidence.currency,
                receipt=evidence.receipt,
                status=evidence.status,
                created_at=evidence.created_at,
            )
        self.remote_refunds[evidence.id] = evidence
        if self.create_failure == "timeout_after_create":
            raise PaymentProviderTimeoutError("create_refund", "ambiguous timeout")
        return evidence

    async def fetch_refund(
        self,
        provider_payment_id: str,
        provider_refund_id: str,
    ) -> ProviderRefund:
        self.fetch_calls.append((provider_payment_id, provider_refund_id))
        return self.remote_refunds[provider_refund_id]

    async def fetch_refunds_for_payment(
        self,
        provider_payment_id: str,
    ) -> tuple[ProviderRefund, ...]:
        self.list_calls.append(provider_payment_id)
        return tuple(
            refund
            for refund in self.remote_refunds.values()
            if refund.payment_id == provider_payment_id
        )

    def set_status(self, status: Literal["pending", "processed", "failed"]) -> None:
        self.remote_refunds = {
            refund_id: ProviderRefund(
                id=refund.id,
                payment_id=refund.payment_id,
                amount=refund.amount,
                currency=refund.currency,
                receipt=refund.receipt,
                status=status,
                created_at=refund.created_at,
            )
            for refund_id, refund in self.remote_refunds.items()
        }

    @staticmethod
    def _evidence(
        request: CreateProviderRefund,
        *,
        status: Literal["pending", "processed", "failed"],
    ) -> ProviderRefund:
        return ProviderRefund(
            id=f"rfnd_{uuid.uuid4().hex}",
            payment_id=request.payment_id,
            amount=request.amount,
            currency=request.currency,
            receipt=request.receipt,
            status=status,
            created_at=NOW,
        )


@pytest.fixture(scope="module")
def refund_database() -> Iterator[IsolatedDatabase]:
    isolated = asyncio.run(_prepare_isolated_database())
    yield isolated
    asyncio.run(isolated.database.dispose())
    asyncio.run(_drop_isolated_schema(isolated.database_url, isolated.schema))


@pytest.fixture(scope="module")
def refund_client(refund_database: IsolatedDatabase) -> Iterator[TestClient]:
    settings = Settings(
        database_url="postgresql://unused:unused@localhost:5432/unused",
        redis_url="redis://localhost:6379/15",
        cors_allowed_origins=["http://localhost:3000"],
        _env_file=None,
    )
    readiness = ReadinessService(
        postgresql_probe=healthy_probe,
        redis_probe=healthy_probe,
        timeout_seconds=0.1,
    )
    with TestClient(
        create_app(
            settings=settings,
            readiness_service=readiness,
            database=refund_database.database,
        )
    ) as client:
        yield client


def _refund_settings(*, retry_seconds: int = 5) -> Settings:
    return _worker_settings(retry_seconds=retry_seconds).model_copy(
        update={
            "refunds_enabled": True,
            "refund_outbox_lease_seconds": 30,
            "refund_outbox_retry_base_seconds": retry_seconds,
            "refund_outbox_retry_max_seconds": retry_seconds,
        }
    )


async def _defer_existing_refund_work(isolated: IsolatedDatabase) -> None:
    async with isolated.database.session() as session:
        pending = await session.scalars(
            select(RefundOutboxEvent).where(RefundOutboxEvent.processed_at.is_(None))
        )
        for event in pending:
            event.available_at = max(event.created_at, datetime(2099, 1, 1, tzinfo=UTC))
            event.processing_started_at = None
            event.lease_expires_at = None
        await session.commit()


def _create_approved_case(
    client: TestClient,
    isolated: IsolatedDatabase,
    *,
    label: str,
    commit_failure: bool = True,
    refund_on_fulfillment_failure: bool = True,
    case_mutator: Callable[[CompensationCase], None] | None = None,
    force_eligibility_check: bool = False,
) -> ApprovedCaseFixture:
    asyncio.run(_defer_existing_work(isolated))
    asyncio.run(_defer_existing_refund_work(isolated))
    evidence = _create_purchase_evidence(
        client,
        isolated,
        label=label,
        refund_on_fulfillment_failure=refund_on_fulfillment_failure,
    )
    payment = asyncio.run(_persist_payment(isolated, evidence, final_state="paid"))
    assert payment.provider_payment_id is not None
    assert payment.outbox_created_at is not None
    entitlement_now = payment.outbox_created_at + timedelta(seconds=1)
    entitlement_cycle = asyncio.run(
        EntitlementOutboxWorker(
            isolated.database,
            _worker_settings(),
            ExactPaymentProvider(payment),
            clock=lambda: entitlement_now,
        ).run_once()
    )
    assert (entitlement_cycle.claimed, entitlement_cycle.processed) == (True, True)

    async def persist_terminal_failure() -> ApprovedCaseFixture:
        async with isolated.database.session() as session:
            entitlement = await session.scalar(
                select(Entitlement).where(Entitlement.transaction_id == payment.transaction_id)
            )
            assert entitlement is not None
            started_at = entitlement_now + timedelta(seconds=1)
            failed_at = started_at + timedelta(seconds=1)
            execution = FulfillmentExecution(
                id=new_fulfillment_execution_id(),
                entitlement_id=entitlement.id,
                account_id=entitlement.account_id,
                transaction_id=entitlement.transaction_id,
                merchant_id=entitlement.merchant_id,
                service_id=entitlement.service_id,
                input_hash=entitlement.input_hash,
                fulfillment_config_id=None,
                fulfillment_config_revision=None,
                provider_type=None,
                endpoint_url=None,
                request_timeout_seconds=None,
                maximum_attempts=None,
                execution_state=FulfillmentExecutionState.PERMANENT_FAILURE,
                attempt_count=1,
                started_at=started_at,
                completed_at=None,
                failed_at=failed_at,
                result_content_type=None,
                result_json=None,
                result_hash=None,
                result_size_bytes=None,
                failure_code="FULFILLMENT_PROVIDER_PERMANENT_FAILURE",
                compensation_required=True,
                revision=1,
                lease_generation=1,
                lease_expires_at=None,
                created_at=started_at,
                updated_at=failed_at,
            )
            session.add(execution)
            await session.flush()
            case = await CompensationApplicationService(
                session, clock=lambda: failed_at
            ).stage_for_permanent_failure(execution, occurred_at=failed_at)
            assert case is not None
            if case_mutator is not None:
                case_mutator(case)
            if force_eligibility_check:
                await session.flush()
                await session.execute(
                    text("SET CONSTRAINTS trg_compensation_cases_validate_evidence IMMEDIATE")
                )
            if commit_failure:
                await session.commit()
            else:
                await session.rollback()
            return ApprovedCaseFixture(
                case_id=case.id,
                transaction_id=payment.transaction_id,
                payment_attempt_id=case.payment_attempt_id,
                provider_order_id=payment.provider_order_id,
                provider_payment_id=payment.provider_payment_id,
                amount=payment.amount,
                currency=payment.currency,
                outbox_available_at=failed_at,
            )

    return asyncio.run(persist_terminal_failure())


async def _read_case_refund(
    isolated: IsolatedDatabase,
    case_id: str,
) -> tuple[CompensationCase, PaymentRefund | None, RefundOutboxEvent | None, int]:
    async with isolated.database.session() as session:
        case = await CompensationCaseRepository(session).get(case_id)
        assert case is not None
        refund = await PaymentRefundRepository(session).get_by_compensation_case_id(case_id)
        outbox = await RefundOutboxEventRepository(session).get_by_deduplication_key(
            f"refund:{case_id}"
        )
        event_count = await session.scalar(
            select(func.count(CompensationEvent.id)).where(
                CompensationEvent.compensation_case_id == case_id
            )
        )
        return case, refund, outbox, int(event_count or 0)


def _webhook_payload(
    fixture: ApprovedCaseFixture,
    refund: PaymentRefund,
    *,
    event_id: str,
    status: Literal["pending", "processed", "failed"],
    received_at: datetime,
) -> VerifiedWebhookPayload:
    event_type = {
        "pending": "refund.created",
        "processed": "refund.processed",
        "failed": "refund.failed",
    }[status]
    body = {
        "entity": "event",
        "event": event_type,
        "created_at": int(received_at.timestamp()),
        "payload": {
            "refund": {
                "entity": {
                    "id": refund.provider_refund_id,
                    "payment_id": fixture.provider_payment_id,
                    "amount": fixture.amount,
                    "currency": fixture.currency,
                    "receipt": refund.provider_receipt,
                    "status": status,
                    "created_at": int(received_at.timestamp()),
                }
            },
            "payment": {
                "entity": {
                    "id": fixture.provider_payment_id,
                    "order_id": fixture.provider_order_id,
                    "amount": fixture.amount,
                    "amount_refunded": fixture.amount,
                    "currency": fixture.currency,
                }
            },
        },
    }
    return VerifiedWebhookPayload(
        provider_event_id=event_id,
        received_at=received_at,
        raw_body=json.dumps(body, separators=(",", ":")).encode(),
    )


def test_permanent_failure_stages_case_events_and_outbox_atomically(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="compensation-atomic-rollback",
        commit_failure=False,
    )

    async def assert_rolled_back() -> None:
        async with refund_database.database.session() as session:
            assert await session.get(CompensationCase, fixture.case_id) is None
            assert (
                await RefundOutboxEventRepository(session).get_by_deduplication_key(
                    f"refund:{fixture.case_id}"
                )
                is None
            )
            assert (
                await session.scalar(
                    select(func.count(CompensationEvent.id)).where(
                        CompensationEvent.compensation_case_id == fixture.case_id
                    )
                )
                == 0
            )

    asyncio.run(assert_rolled_back())

    committed = _create_approved_case(
        refund_client,
        refund_database,
        label="compensation-atomic-commit",
    )
    case, refund, outbox, event_count = asyncio.run(
        _read_case_refund(refund_database, committed.case_id)
    )
    assert CompensationDecisionState(case.decision_state) is CompensationDecisionState.APPROVED
    assert case.approved_refund_amount == committed.amount
    assert refund is None
    assert outbox is not None
    assert outbox.deduplication_key == f"refund:{committed.case_id}"
    assert outbox.payload == {
        "compensation_case_id": committed.case_id,
        "transaction_id": committed.transaction_id,
        "approved_refund_amount": committed.amount,
        "currency": committed.currency,
    }
    assert event_count == 4


@pytest.mark.parametrize(
    ("provider_status", "expected_refund_state", "expected_case_state", "terminal"),
    [
        (
            "pending",
            PaymentRefundState.REFUND_PROCESSING,
            CompensationDecisionState.EXECUTING,
            False,
        ),
        ("processed", PaymentRefundState.REFUNDED, CompensationDecisionState.COMPLETED, True),
        ("failed", PaymentRefundState.REFUND_FAILED, CompensationDecisionState.MANUAL_REVIEW, True),
    ],
)
def test_refund_execution_converges_provider_statuses_and_reuses_exact_receipt(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
    provider_status: Literal["pending", "processed", "failed"],
    expected_refund_state: PaymentRefundState,
    expected_case_state: CompensationDecisionState,
    terminal: bool,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label=f"refund-status-{provider_status}",
    )
    provider = ScriptedRefundProvider(create_status=provider_status)

    async def execute() -> object:
        async with refund_database.database.session() as session:
            return await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW,
            ).execute_case(fixture.case_id)

    result = asyncio.run(execute())
    case, refund, _outbox, _events = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert result.terminal is terminal
    assert refund is not None
    assert PaymentRefundState(refund.refund_state) is expected_refund_state
    assert CompensationDecisionState(case.decision_state) is expected_case_state
    assert len(provider.create_requests) == 1
    request = provider.create_requests[0]
    assert request.receipt == refund.id == refund.provider_receipt
    assert request.payment_id == fixture.provider_payment_id
    assert (request.amount, request.currency) == (fixture.amount, fixture.currency)
    if provider_status == "failed":
        assert case.decision_provenance is CompensationDecisionProvenance.AUTOMATIC_APPROVED
        assert case.approved_refund_amount == fixture.amount


def test_completed_refund_has_exact_ordered_append_only_audit_chain(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-complete-audit-chain",
    )
    provider = ScriptedRefundProvider(create_status="processed")

    async def execute_and_read() -> list[CompensationEvent]:
        async with refund_database.database.session() as session:
            await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW,
            ).execute_case(fixture.case_id)
        async with refund_database.database.session() as session:
            return list(
                await session.scalars(
                    select(CompensationEvent)
                    .where(CompensationEvent.compensation_case_id == fixture.case_id)
                    .order_by(CompensationEvent.sequence)
                )
            )

    events = asyncio.run(execute_and_read())
    assert [event.sequence for event in events] == list(range(1, 10))
    assert [event.event_type for event in events] == [
        CompensationEventType.COMPENSATION_CASE_CREATED,
        CompensationEventType.COMPENSATION_RECOMMENDED,
        CompensationEventType.COMPENSATION_APPROVED,
        CompensationEventType.REFUND_OUTBOX_CREATED,
        CompensationEventType.REFUND_REQUESTED,
        CompensationEventType.REFUND_REQUEST_STARTED,
        CompensationEventType.RAZORPAY_REFUND_CREATED,
        CompensationEventType.REFUND_COMPLETED,
        CompensationEventType.COMPENSATION_CLOSED,
    ]


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        ("timeout_after_create", PaymentRefundState.REFUND_UNCERTAIN),
        ("mismatch", PaymentRefundState.RECONCILIATION_REQUIRED),
    ],
)
def test_timeout_and_mismatch_quarantine_reserved_refund(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
    failure: Literal["timeout_after_create", "mismatch"],
    expected_state: PaymentRefundState,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label=f"refund-quarantine-{failure}",
    )
    provider = ScriptedRefundProvider(create_failure=failure)

    async def execute() -> None:
        async with refund_database.database.session() as session:
            service = RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW,
            )
            expected = (
                CompensationTimeoutError
                if failure == "timeout_after_create"
                else CompensationIntegrityError
            )
            with pytest.raises(expected):
                await service.execute_case(fixture.case_id)

    asyncio.run(execute())
    case, refund, _outbox, _events = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert refund is not None
    assert PaymentRefundState(refund.refund_state) is expected_state
    assert CompensationDecisionState(case.decision_state) is CompensationDecisionState.EXECUTING
    assert len(provider.create_requests) == 1


@pytest.mark.parametrize("provider_amount", [1, 501])
def test_unreserved_or_overrun_provider_refund_blocks_local_creation(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
    provider_amount: int,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label=f"refund-external-conflict-{provider_amount}",
    )
    provider = ScriptedRefundProvider()
    external = ProviderRefund(
        id=f"rfnd_{uuid.uuid4().hex}",
        payment_id=fixture.provider_payment_id,
        amount=provider_amount,
        currency=fixture.currency,
        receipt=f"external-refund-{provider_amount}",
        status="processed",
        created_at=NOW,
    )
    provider.remote_refunds[external.id] = external

    async def execute() -> None:
        async with refund_database.database.session() as session:
            with pytest.raises(CompensationIntegrityError) as error_info:
                await RefundApplicationService(
                    session,
                    provider,
                    _refund_settings(),
                    clock=lambda: NOW,
                ).execute_case(fixture.case_id)
            assert error_info.value.reason_code == "REFUND_PROVIDER_RESPONSE_MISMATCH"

    asyncio.run(execute())
    _case, refund, _outbox, _events = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert refund is not None
    assert refund.refund_state is PaymentRefundState.RECONCILIATION_REQUIRED
    assert provider.create_requests == []


def test_ambiguous_create_recovers_by_stable_receipt_without_duplicate_execution(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-receipt-recovery",
    )
    provider = ScriptedRefundProvider(
        create_status="pending",
        create_failure="timeout_after_create",
    )

    async def first_attempt() -> None:
        async with refund_database.database.session() as session:
            with pytest.raises(CompensationTimeoutError):
                await RefundApplicationService(
                    session,
                    provider,
                    _refund_settings(),
                    clock=lambda: NOW,
                ).execute_case(fixture.case_id)

    asyncio.run(first_attempt())
    assert len(provider.create_requests) == 1
    provider.create_failure = None
    provider.set_status("processed")

    async def recover_and_repeat() -> tuple[object, object]:
        async with refund_database.database.session() as session:
            service = RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW + timedelta(seconds=10),
            )
            recovered = await service.execute_case(fixture.case_id)
        calls_after_recovery = (
            len(provider.create_requests),
            len(provider.list_calls),
            len(provider.fetch_calls),
        )
        async with refund_database.database.session() as session:
            duplicate = await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW + timedelta(seconds=20),
            ).execute_case(fixture.case_id)
        return recovered, (duplicate, calls_after_recovery)

    recovered, duplicate_data = asyncio.run(recover_and_repeat())
    duplicate, calls_after_recovery = duplicate_data
    assert recovered.terminal and duplicate.terminal
    assert recovered.refund_id == duplicate.refund_id
    assert len(provider.create_requests) == 1
    assert calls_after_recovery[1] == 2
    assert calls_after_recovery[2] == 0
    assert (len(provider.list_calls), len(provider.fetch_calls)) == (2, 0)


def test_zero_match_after_ambiguous_create_never_issues_second_refund(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-zero-match-quarantine",
    )
    provider = ScriptedRefundProvider(create_failure="timeout_after_create")

    async def first_attempt() -> None:
        async with refund_database.database.session() as session:
            with pytest.raises(CompensationTimeoutError):
                await RefundApplicationService(
                    session,
                    provider,
                    _refund_settings(),
                    clock=lambda: NOW,
                ).execute_case(fixture.case_id)

    asyncio.run(first_attempt())
    assert len(provider.create_requests) == 1
    provider.remote_refunds.clear()  # model a lagging or incomplete provider list response
    provider.create_failure = None

    async def retry() -> None:
        async with refund_database.database.session() as session:
            with pytest.raises(CompensationUnavailableError) as error:
                await RefundApplicationService(
                    session,
                    provider,
                    _refund_settings(),
                    clock=lambda: NOW + timedelta(seconds=10),
                ).execute_case(fixture.case_id)
            assert error.value.reason_code == "REFUND_CREATION_UNCERTAIN"

    asyncio.run(retry())
    assert len(provider.create_requests) == 1
    _case, refund, _outbox, _events = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert refund is not None
    assert refund.refund_state is PaymentRefundState.REFUND_UNCERTAIN


def test_reconcile_never_attempted_pending_reservation_is_a_noop(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-pending-reconcile-noop",
    )
    provider = ScriptedRefundProvider()

    async def reserve_then_reconcile() -> PaymentRefund:
        async with refund_database.database.session() as session:
            refund = await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW,
            )._prepare_refund(fixture.case_id)  # noqa: SLF001
            refund_id = refund.id
        async with refund_database.database.session() as session:
            return await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW + timedelta(seconds=1),
            ).reconcile_refund(refund_id)

    refund = asyncio.run(reserve_then_reconcile())
    assert refund.refund_state is PaymentRefundState.REFUND_PENDING
    assert refund.requested_at is None
    assert refund.reconciliation_required_at is None
    assert provider.create_requests == []


def test_webhook_is_idempotent_and_late_pending_evidence_cannot_regress_terminal_refund(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-webhook-monotonic",
    )
    provider = ScriptedRefundProvider(create_status="pending")

    async def execute_pending() -> None:
        async with refund_database.database.session() as session:
            await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW,
            ).execute_case(fixture.case_id)

    asyncio.run(execute_pending())
    _case, pending, _outbox, _events = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert pending is not None
    processed_payload = _webhook_payload(
        fixture,
        pending,
        event_id="evt_refund_processed",
        status="processed",
        received_at=NOW + timedelta(seconds=10),
    )

    async def apply(payload: VerifiedWebhookPayload) -> None:
        async with refund_database.database.session() as session:
            await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW + timedelta(seconds=10),
            ).process_refund_webhook(payload)

    asyncio.run(apply(processed_payload))
    _case, processed, _outbox, count_after_processed = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert processed is not None
    revision_after_processed = processed.revision
    asyncio.run(apply(processed_payload))
    _case, duplicate, _outbox, count_after_duplicate = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert duplicate is not None
    assert (duplicate.refund_state, duplicate.revision, count_after_duplicate) == (
        PaymentRefundState.REFUNDED,
        revision_after_processed,
        count_after_processed,
    )

    late_pending = _webhook_payload(
        fixture,
        duplicate,
        event_id="evt_refund_late_pending",
        status="pending",
        received_at=NOW + timedelta(seconds=20),
    )
    asyncio.run(apply(late_pending))
    _case, final, _outbox, _events = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert final is not None
    assert (final.refund_state, final.revision) == (
        PaymentRefundState.REFUNDED,
        revision_after_processed,
    )


def test_terminal_contradiction_is_durable_overlay_until_exact_api_resolution(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-terminal-contradiction-overlay",
    )
    provider = ScriptedRefundProvider(create_status="pending")

    async def execute_then_conflict() -> tuple[PaymentRefund, str, int]:
        async with refund_database.database.session() as session:
            pending = await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW,
            ).execute_case(fixture.case_id)
        provider.set_status("processed")
        async with refund_database.database.session() as session:
            await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW + timedelta(seconds=5),
            ).reconcile_refund(pending.refund_id)
        _case, completed, _outbox, _events = await _read_case_refund(
            refund_database, fixture.case_id
        )
        assert completed is not None and completed.provider_refund_id is not None
        conflict = ProviderRefund(
            id=completed.provider_refund_id,
            payment_id=completed.provider_payment_id,
            amount=completed.amount,
            currency=completed.currency,
            receipt=completed.provider_receipt,
            status="failed",
            created_at=NOW,
        )
        async with refund_database.database.session() as session:
            with pytest.raises(CompensationIntegrityError) as error_info:
                await RefundApplicationService(
                    session,
                    provider,
                    _refund_settings(),
                    clock=lambda: NOW + timedelta(seconds=10),
                ).apply_provider_evidence(
                    completed.id,
                    conflict,
                    actor_type=CompensationEventActorType.PROVIDER_WEBHOOK,
                    actor_id="evt_terminal_conflict",
                    idempotency_key="webhook:evt_terminal_conflict",
                    observed_at=NOW + timedelta(seconds=10),
                )
            assert error_info.value.reason_code == "REFUND_RECONCILIATION_REQUIRED"
        async with refund_database.database.session() as session:
            service = CompensationApplicationService(session)
            projection = await service.projection_for_transaction(
                fixture.transaction_id,
                payment_state="paid",
            )
            required_count = await session.scalar(
                select(func.count(CompensationEvent.id)).where(
                    CompensationEvent.compensation_case_id == fixture.case_id,
                    CompensationEvent.event_type
                    == CompensationEventType.REFUND_RECONCILIATION_REQUIRED,
                )
            )
        return completed, projection.commerce_outcome, int(required_count or 0)

    completed, outcome, required_count = asyncio.run(execute_then_conflict())
    _case, quarantined, _outbox, _events = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert quarantined is not None
    assert quarantined.refund_state is PaymentRefundState.REFUNDED
    assert quarantined.provider_status == "processed"
    assert quarantined.reconciliation_reason_code == "REFUND_TERMINAL_EVIDENCE_CONFLICT"
    assert quarantined.reconciliation_required_at is not None
    assert outcome == "manual_review"
    assert required_count == 1

    provider.set_status("processed")

    async def resolve() -> tuple[PaymentRefund, int]:
        async with refund_database.database.session() as session:
            resolved = await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW + timedelta(seconds=20),
            ).reconcile_refund(completed.id)
        async with refund_database.database.session() as session:
            resolved_count = await session.scalar(
                select(func.count(CompensationEvent.id)).where(
                    CompensationEvent.compensation_case_id == fixture.case_id,
                    CompensationEvent.event_type
                    == CompensationEventType.REFUND_RECONCILIATION_RESOLVED,
                )
            )
        return resolved, int(resolved_count or 0)

    resolved, resolved_count = asyncio.run(resolve())
    assert resolved.refund_state is PaymentRefundState.REFUNDED
    assert resolved.reconciliation_required_at is None
    assert resolved.reconciliation_reason_code is None
    assert resolved.last_reconciled_at == NOW + timedelta(seconds=20)
    assert resolved_count == 1


def test_refund_worker_retries_pending_then_marks_processed_without_duplicate_create(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-worker-pending-processed",
    )
    provider = ScriptedRefundProvider(create_status="pending")
    first_now = fixture.outbox_available_at + timedelta(seconds=1)
    first = asyncio.run(
        RefundOutboxWorker(
            refund_database.database,
            _refund_settings(retry_seconds=5),
            provider,
            clock=lambda: first_now,
        ).run_once()
    )
    assert (first.claimed, first.processed, first.reason_code) == (
        True,
        False,
        "REFUND_PROCESSING",
    )
    _case, refund, outbox, _events = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert refund is not None and outbox is not None
    assert outbox.available_at == first_now + timedelta(seconds=5)
    assert outbox.attempt_count == 1
    provider.set_status("processed")
    second = asyncio.run(
        RefundOutboxWorker(
            refund_database.database,
            _refund_settings(retry_seconds=5),
            provider,
            clock=lambda: outbox.available_at,
        ).run_once()
    )
    duplicate_poll = asyncio.run(
        RefundOutboxWorker(
            refund_database.database,
            _refund_settings(retry_seconds=5),
            provider,
            clock=lambda: outbox.available_at + timedelta(seconds=1),
        ).run_once()
    )
    assert (second.claimed, second.processed, second.reason_code) == (
        True,
        True,
        "REFUND_COMPLETED",
    )
    assert (duplicate_poll.claimed, duplicate_poll.processed) == (False, False)
    assert len(provider.create_requests) == 1
    assert len(provider.list_calls) == 2
    assert len(provider.fetch_calls) == 0
    case, final, final_outbox, _events = asyncio.run(
        _read_case_refund(refund_database, fixture.case_id)
    )
    assert final is not None and final_outbox is not None
    assert final.refund_state is PaymentRefundState.REFUNDED
    assert case.decision_state is CompensationDecisionState.COMPLETED
    assert final_outbox.processed_at is not None
    assert final_outbox.attempt_count == 2


def test_two_postgresql_workers_cannot_claim_one_refund_side_effect(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-worker-concurrent-claim",
    )

    async def exercise() -> tuple[object, object, ScriptedRefundProvider]:
        class BlockingLookupProvider(ScriptedRefundProvider):
            def __init__(self) -> None:
                super().__init__(create_status="processed")
                self.lookup_started = asyncio.Event()
                self.release_lookup = asyncio.Event()

            async def fetch_refunds_for_payment(
                self,
                provider_payment_id: str,
            ) -> tuple[ProviderRefund, ...]:
                self.lookup_started.set()
                await self.release_lookup.wait()
                return await super().fetch_refunds_for_payment(provider_payment_id)

        provider = BlockingLookupProvider()
        now = fixture.outbox_available_at + timedelta(seconds=1)
        first_worker = RefundOutboxWorker(
            refund_database.database,
            _refund_settings(),
            provider,
            clock=lambda: now,
        )
        second_worker = RefundOutboxWorker(
            refund_database.database,
            _refund_settings(),
            provider,
            clock=lambda: now,
        )
        first_task = asyncio.create_task(first_worker.run_once())
        await provider.lookup_started.wait()
        second = await second_worker.run_once()
        provider.release_lookup.set()
        first = await first_task
        return first, second, provider

    first, second, provider = asyncio.run(exercise())
    assert (first.claimed, first.processed, first.reason_code) == (
        True,
        True,
        "REFUND_COMPLETED",
    )
    assert (second.claimed, second.processed) == (False, False)
    assert len(provider.create_requests) == 1
    case, refund, outbox, _events = asyncio.run(_read_case_refund(refund_database, fixture.case_id))
    assert refund is not None and outbox is not None
    assert refund.refund_state is PaymentRefundState.REFUNDED
    assert case.decision_state is CompensationDecisionState.COMPLETED
    assert outbox.processed_at is not None


def test_two_simultaneous_refund_reconciliations_converge_consistently(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-concurrent-reconciliation",
    )

    async def exercise() -> tuple[list[PaymentRefund], ScriptedRefundProvider]:
        class BarrierFetchProvider(ScriptedRefundProvider):
            def __init__(self) -> None:
                super().__init__(create_status="pending")
                self.block_fetches = False
                self.fetch_count = 0
                self.both_fetches_started = asyncio.Event()
                self.release_fetches = asyncio.Event()

            async def fetch_refunds_for_payment(
                self,
                provider_payment_id: str,
            ) -> tuple[ProviderRefund, ...]:
                if self.block_fetches:
                    self.fetch_count += 1
                    if self.fetch_count == 2:
                        self.both_fetches_started.set()
                    await self.release_fetches.wait()
                return await super().fetch_refunds_for_payment(provider_payment_id)

        provider = BarrierFetchProvider()
        async with refund_database.database.session() as session:
            pending = await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW,
            ).execute_case(fixture.case_id)
            assert pending.state is PaymentRefundState.REFUND_PROCESSING
            refund_id = pending.refund_id

        provider.set_status("processed")
        provider.block_fetches = True

        async def reconcile() -> PaymentRefund:
            async with refund_database.database.session() as session:
                return await RefundApplicationService(
                    session,
                    provider,
                    _refund_settings(),
                    clock=lambda: NOW + timedelta(seconds=10),
                ).reconcile_refund(refund_id)

        first_task = asyncio.create_task(reconcile())
        second_task = asyncio.create_task(reconcile())
        await provider.both_fetches_started.wait()
        provider.release_fetches.set()
        return list(await asyncio.gather(first_task, second_task)), provider

    reconciled, provider = asyncio.run(exercise())
    assert {refund.id for refund in reconciled} == {reconciled[0].id}
    assert all(refund.refund_state is PaymentRefundState.REFUNDED for refund in reconciled)
    assert provider.fetch_count == 2

    async def completed_event_count() -> int:
        async with refund_database.database.session() as session:
            count = await session.scalar(
                select(func.count(CompensationEvent.id)).where(
                    CompensationEvent.compensation_case_id == fixture.case_id,
                    CompensationEvent.event_type == CompensationEventType.REFUND_COMPLETED,
                )
            )
            return int(count or 0)

    assert asyncio.run(completed_event_count()) == 1


@pytest.mark.parametrize(
    ("failure", "processed", "reason_code", "refund_state"),
    [
        ("rejected", True, "REFUND_CREATION_FAILED", PaymentRefundState.REFUND_FAILED),
        (
            "timeout_after_create",
            False,
            "REFUND_CREATION_UNCERTAIN",
            PaymentRefundState.REFUND_UNCERTAIN,
        ),
        (
            "mismatch",
            False,
            "REFUND_PROVIDER_RESPONSE_MISMATCH",
            PaymentRefundState.RECONCILIATION_REQUIRED,
        ),
    ],
)
def test_refund_worker_terminal_and_retry_handling(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
    failure: Literal["rejected", "timeout_after_create", "mismatch"],
    processed: bool,
    reason_code: str,
    refund_state: PaymentRefundState,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label=f"refund-worker-{failure}",
    )
    provider = ScriptedRefundProvider(create_failure=failure)
    now = fixture.outbox_available_at + timedelta(seconds=1)
    result = asyncio.run(
        RefundOutboxWorker(
            refund_database.database,
            _refund_settings(retry_seconds=7),
            provider,
            clock=lambda: now,
        ).run_once()
    )
    assert (result.claimed, result.processed, result.reason_code) == (
        True,
        processed,
        reason_code,
    )
    case, refund, outbox, _events = asyncio.run(_read_case_refund(refund_database, fixture.case_id))
    assert refund is not None and outbox is not None
    assert refund.refund_state is refund_state
    assert (outbox.processed_at is not None) is processed
    if processed:
        assert case.decision_state is CompensationDecisionState.MANUAL_REVIEW
    else:
        assert outbox.available_at == now + timedelta(seconds=7)
        assert outbox.last_error_code == reason_code


def _manual_refund(
    fixture: ApprovedCaseFixture,
    *,
    amount: int,
    state: PaymentRefundState,
    now: datetime,
) -> tuple[PaymentRefund, CompensationEvent]:
    refund_id = new_payment_refund_id()
    terminal = state is PaymentRefundState.REFUNDED
    refund = PaymentRefund(
        id=refund_id,
        compensation_case_id=fixture.case_id,
        transaction_id=fixture.transaction_id,
        payment_attempt_id=fixture.payment_attempt_id,
        provider=PaymentProvider.RAZORPAY,
        provider_payment_id=fixture.provider_payment_id,
        provider_refund_id=f"rfnd_{uuid.uuid4().hex}" if terminal else None,
        amount=amount,
        currency=fixture.currency,
        refund_state=state,
        provider_status="processed" if terminal else None,
        provider_receipt=refund_id,
        created_at=now,
        requested_at=now if terminal else None,
        processed_at=now if terminal else None,
        failed_at=None,
        last_reconciled_at=None,
        revision=1,
    )
    event = CompensationEvent(
        id=new_compensation_event_id(),
        compensation_case_id=fixture.case_id,
        transaction_id=fixture.transaction_id,
        payment_refund_id=refund.id,
        sequence=100 if terminal else 101,
        case_revision=2,
        refund_revision=1,
        event_type=(
            CompensationEventType.REFUND_COMPLETED
            if terminal
            else CompensationEventType.REFUND_REQUESTED
        ),
        actor_type=CompensationEventActorType.OPERATOR,
        actor_id="postgres-concurrency-test",
        reason_code="REFUND_COMPLETED" if terminal else "REFUND_REQUESTED",
        event_metadata={"test": "aggregate-reservation"},
        idempotency_key=f"aggregate-reservation:{refund.id}",
        occurred_at=now,
    )
    return refund, event


def test_postgresql_concurrent_refund_reservations_cannot_overrun_approved_amount(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-concurrent-overrun",
    )

    async def exercise() -> tuple[PaymentRefund, Exception | None]:
        first_session = refund_database.database.session()
        second_session = refund_database.database.session()
        async with first_session as first, second_session as second:
            case = await CompensationCaseRepository(first).get_for_update(fixture.case_id)
            assert case is not None
            case.decision_state = CompensationDecisionState.EXECUTING
            case.revision = 2
            first.add(
                CompensationEvent(
                    id=new_compensation_event_id(),
                    compensation_case_id=case.id,
                    transaction_id=case.transaction_id,
                    payment_refund_id=None,
                    sequence=99,
                    case_revision=2,
                    refund_revision=None,
                    event_type=CompensationEventType.COMPENSATION_APPROVED,
                    actor_type=CompensationEventActorType.OPERATOR,
                    actor_id="postgres-concurrency-test",
                    reason_code="COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED",
                    event_metadata={"test": "executing-transition"},
                    idempotency_key=f"aggregate-executing:{case.id}",
                    occurred_at=NOW,
                )
            )
            await first.commit()

            first_refund, first_event = _manual_refund(
                fixture,
                amount=fixture.amount * 3 // 5,
                state=PaymentRefundState.REFUNDED,
                now=NOW + timedelta(seconds=1),
            )
            second_refund, second_event = _manual_refund(
                fixture,
                amount=fixture.amount * 3 // 5,
                state=PaymentRefundState.REFUND_PENDING,
                now=NOW + timedelta(seconds=2),
            )
            first.add(first_refund)
            await first.flush()
            first.add(first_event)

            second_started = asyncio.Event()

            async def commit_second() -> Exception | None:
                second.add(second_refund)
                second_started.set()
                try:
                    await second.flush()
                    second.add(second_event)
                    await second.commit()
                except (DBAPIError, IntegrityError) as error:
                    await second.rollback()
                    return error
                return None

            second_task = asyncio.create_task(commit_second())
            await second_started.wait()
            await asyncio.sleep(0.05)
            assert not second_task.done()
            await first.commit()
            second_error = await second_task
            return first_refund, second_error

    first_refund, second_error = asyncio.run(exercise())
    assert second_error is not None
    assert "refund reservations exceed approved compensation amount" in str(second_error)

    async def verify() -> tuple[int, int]:
        async with refund_database.database.session() as session:
            refunds = list(
                await session.scalars(
                    select(PaymentRefund).where(
                        PaymentRefund.compensation_case_id == fixture.case_id
                    )
                )
            )
            total = sum(
                refund.amount
                for refund in refunds
                if refund.refund_state is not PaymentRefundState.REFUND_FAILED
            )
            return len(refunds), total

    assert asyncio.run(verify()) == (1, first_refund.amount)


async def _direct_transition_case_to_executing(
    isolated: IsolatedDatabase,
    fixture: ApprovedCaseFixture,
) -> None:
    async with isolated.database.session() as session:
        await session.execute(
            text(
                """
                UPDATE compensation_cases
                   SET decision_state = 'executing', revision = 2
                 WHERE id = :case_id
                """
            ),
            {"case_id": fixture.case_id},
        )
        session.add(
            CompensationEvent(
                id=new_compensation_event_id(),
                compensation_case_id=fixture.case_id,
                transaction_id=fixture.transaction_id,
                payment_refund_id=None,
                sequence=99,
                case_revision=2,
                refund_revision=None,
                event_type=CompensationEventType.COMPENSATION_APPROVED,
                actor_type=CompensationEventActorType.OPERATOR,
                actor_id="postgres-direct-write-test",
                reason_code="COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED",
                event_metadata={"test": "direct-executing-transition"},
                idempotency_key=f"direct-executing:{fixture.case_id}",
                occurred_at=NOW,
            )
        )
        await session.commit()


def test_postgresql_schema_exposes_terminal_overlay_and_deferred_completion_guard(
    refund_database: IsolatedDatabase,
) -> None:
    async def inspect_schema() -> tuple[set[str], set[str], tuple[bool, bool, str]]:
        async with refund_database.database.engine.connect() as connection:
            columns = await connection.scalars(
                text(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = current_schema()
                       AND table_name = 'payment_refunds'
                    """
                )
            )
            checks = await connection.scalars(
                text(
                    """
                    SELECT constraint_name
                      FROM information_schema.table_constraints
                     WHERE constraint_schema = current_schema()
                       AND table_name = 'payment_refunds'
                       AND constraint_type = 'CHECK'
                    """
                )
            )
            trigger = await connection.execute(
                text(
                    """
                    SELECT trigger.tgdeferrable, trigger.tginitdeferred,
                           pg_get_triggerdef(trigger.oid)
                      FROM pg_trigger trigger
                      JOIN pg_class relation ON relation.oid = trigger.tgrelid
                     WHERE relation.relnamespace = current_schema()::regnamespace
                       AND relation.relname = 'compensation_cases'
                       AND trigger.tgname =
                           'trg_compensation_cases_completed_refund_total'
                    """
                )
            )
            row = trigger.one()
            return set(columns), set(checks), (row[0], row[1], row[2])

    columns, checks, trigger = asyncio.run(inspect_schema())
    assert {"reconciliation_required_at", "reconciliation_reason_code"} <= columns
    assert {
        "ck_payment_refunds_terminal_provider_evidence",
        "ck_payment_refunds_reconciliation_overlay_pair",
        "ck_payment_refunds_reconciliation_required_after_request",
        "ck_payment_refunds_reconciliation_reason_code_valid",
        "ck_payment_refunds_reconciliation_reason_code_format",
    } <= checks
    assert trigger[:2] == (True, True)
    assert "DEFERRABLE INITIALLY DEFERRED" in trigger[2]


def test_postgresql_compensation_eligibility_guard_is_deferred_and_policy_bound(
    refund_database: IsolatedDatabase,
) -> None:
    async def inspect_guard() -> tuple[bool, bool, str, str]:
        async with refund_database.database.engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT trigger.tgdeferrable, trigger.tginitdeferred,
                           pg_get_triggerdef(trigger.oid),
                           pg_get_functiondef(trigger.tgfoid)
                      FROM pg_trigger trigger
                      JOIN pg_class relation ON relation.oid = trigger.tgrelid
                     WHERE relation.relnamespace = current_schema()::regnamespace
                       AND relation.relname = 'compensation_cases'
                       AND trigger.tgname =
                           'trg_compensation_cases_validate_evidence'
                    """
                )
            )
            row = result.one()
            return row[0], row[1], row[2], row[3]

    deferrable, initially_deferred, trigger_sql, function_sql = asyncio.run(inspect_guard())
    normalized_trigger = " ".join(trigger_sql.lower().split())
    normalized_function = " ".join(function_sql.lower().split())
    assert (deferrable, initially_deferred) == (True, True)
    assert "after insert or update" in normalized_trigger
    assert "execution.failure_code = new.failure_code" in normalized_function
    assert "quote.refund_on_fulfillment_failure" in normalized_function


@pytest.mark.parametrize(
    ("conflict_kind", "preexisting_unmatched"),
    [("payment_entities", True), ("payment_order", False)],
)
def test_postgresql_signed_refund_identity_conflict_is_atomically_quarantined(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
    conflict_kind: str,
    preexisting_unmatched: bool,
) -> None:
    first = _create_approved_case(
        refund_client,
        refund_database,
        label=f"signed-refund-identity-conflict-first-{conflict_kind}",
    )
    second = _create_approved_case(
        refund_client,
        refund_database,
        label=f"signed-refund-identity-conflict-second-{conflict_kind}",
    )
    received_at = max(first.outbox_available_at, second.outbox_available_at) + timedelta(seconds=3)
    provider_event_id = f"evt_refund_identity_conflict_{uuid.uuid4().hex}"
    payment_order_id = (
        second.provider_order_id if conflict_kind == "payment_order" else first.provider_order_id
    )
    refund_payment_id = (
        second.provider_payment_id
        if conflict_kind == "payment_entities"
        else first.provider_payment_id
    )
    body = {
        "entity": "event",
        "event": "refund.processed",
        "created_at": int(received_at.timestamp()),
        "payload": {
            "payment": {
                "entity": {
                    "id": first.provider_payment_id,
                    "order_id": payment_order_id,
                }
            },
            "refund": {
                "entity": {
                    "id": f"rfnd_{uuid.uuid4().hex}",
                    "payment_id": refund_payment_id,
                }
            },
        },
    }
    payload = VerifiedWebhookPayload(
        provider_event_id=provider_event_id,
        received_at=received_at,
        raw_body=json.dumps(body, separators=(",", ":")).encode(),
    )

    async def quarantine_and_read() -> tuple[list[PaymentTransaction], int, list[int], str]:
        async with refund_database.database.session() as session:
            if preexisting_unmatched:
                session.add(
                    RazorpayWebhookEvent(
                        id=new_razorpay_webhook_event_id(),
                        provider_event_id=provider_event_id,
                        provider_event_type="invalid",
                        raw_body_hash=sha256_bytes(payload.raw_body),
                        provider_created_at=received_at,
                        received_at=received_at,
                        processed_at=received_at,
                        processing_status=WebhookProcessingStatus.FAILED,
                        processing_reason_code="PAYMENT_WEBHOOK_PAYLOAD_INVALID",
                        provider_order_id=None,
                        provider_payment_id=None,
                        transaction_id=None,
                    )
                )
                await session.commit()
            before = {
                transaction_id: (await session.get(PaymentTransaction, transaction_id)).revision
                for transaction_id in (first.transaction_id, second.transaction_id)
            }
            service = PaymentApplicationService(
                session,
                None,
                _refund_settings(),
                clock=lambda: received_at,
            )
            await service.quarantine_value_revoking_webhook(payload)
            await service.quarantine_value_revoking_webhook(payload)
            transactions = [
                await session.get(PaymentTransaction, transaction_id)
                for transaction_id in (first.transaction_id, second.transaction_id)
            ]
            assert all(transaction is not None for transaction in transactions)
            webhook_count = await session.scalar(
                select(func.count(RazorpayWebhookEvent.id)).where(
                    RazorpayWebhookEvent.provider_event_id == provider_event_id
                )
            )
            anomaly_counts = [
                int(
                    await session.scalar(
                        select(func.count(PaymentTransactionEvent.id)).where(
                            PaymentTransactionEvent.transaction_id == transaction_id,
                            PaymentTransactionEvent.reason_code
                            == "PAYMENT_REFUND_EVIDENCE_CONFLICT",
                            PaymentTransactionEvent.actor_id == provider_event_id,
                        )
                    )
                    or 0
                )
                for transaction_id in (first.transaction_id, second.transaction_id)
            ]
            for transaction in transactions:
                assert transaction is not None
                assert transaction.revision == before[transaction.id] + 1
                with pytest.raises(EntitlementConflictError):
                    await service.require_local_value_release_eligibility(transaction.id)
            stored = await session.scalar(
                select(RazorpayWebhookEvent).where(
                    RazorpayWebhookEvent.provider_event_id == provider_event_id
                )
            )
            assert stored is not None
            return (
                [transaction for transaction in transactions if transaction is not None],
                int(webhook_count or 0),
                anomaly_counts,
                stored.processing_reason_code,
            )

    transactions, webhook_count, anomaly_counts, stored_reason = asyncio.run(quarantine_and_read())
    assert [transaction.transaction_state.value for transaction in transactions] == [
        "paid",
        "paid",
    ]
    assert webhook_count == 1
    assert anomaly_counts == [1, 1]
    assert stored_reason == (
        "PAYMENT_WEBHOOK_PAYLOAD_INVALID"
        if preexisting_unmatched
        else "PAYMENT_REFUND_EVIDENCE_CONFLICT"
    )


def test_postgresql_compensation_case_rejects_failure_code_not_on_execution(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    def forge_failure_code(case: CompensationCase) -> None:
        case.failure_code = "DIFFERENT_PERMANENT_FAILURE"

    with pytest.raises(
        IntegrityError,
        match="lacks exact paid terminal-failure evidence",
    ):
        _create_approved_case(
            refund_client,
            refund_database,
            label="compensation-failure-code-mismatch",
            case_mutator=forge_failure_code,
            force_eligibility_check=True,
        )


def test_postgresql_automatic_approval_requires_quote_refund_policy(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    def forge_automatic_approval(case: CompensationCase) -> None:
        assert case.decision_state is CompensationDecisionState.MANUAL_REVIEW
        case.recommended_action = CompensationRecommendedAction.FULL_REFUND
        case.decision_state = CompensationDecisionState.APPROVED
        case.decision_provenance = CompensationDecisionProvenance.AUTOMATIC_APPROVED
        case.approved_refund_amount = case.amount_paid
        case.decision_reason_code = "COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED"

    with pytest.raises(
        IntegrityError,
        match="lacks exact paid terminal-failure evidence",
    ):
        _create_approved_case(
            refund_client,
            refund_database,
            label="automatic-refund-policy-disabled",
            refund_on_fulfillment_failure=False,
            case_mutator=forge_automatic_approval,
            force_eligibility_check=True,
        )


def test_postgresql_compensation_migration_downgrades_and_reupgrades_cleanly() -> None:
    isolated = asyncio.run(_prepare_isolated_database())
    try:

        async def migrate_and_inspect() -> tuple[set[str], set[str], bool]:
            async with isolated.database.engine.begin() as connection:

                def downgrade(sync_connection: object) -> None:
                    config = Config(API_ROOT / "alembic.ini")
                    config.attributes["connection"] = sync_connection
                    command.downgrade(config, "20260826_0008")

                await connection.run_sync(downgrade)
                downgraded_tables = set(
                    await connection.scalars(
                        text(
                            """
                            SELECT table_name
                              FROM information_schema.tables
                             WHERE table_schema = current_schema()
                            """
                        )
                    )
                )

                def upgrade(sync_connection: object) -> None:
                    config = Config(API_ROOT / "alembic.ini")
                    config.attributes["connection"] = sync_connection
                    command.upgrade(config, "head")

                await connection.run_sync(upgrade)
                upgraded_tables = set(
                    await connection.scalars(
                        text(
                            """
                            SELECT table_name
                              FROM information_schema.tables
                             WHERE table_schema = current_schema()
                            """
                        )
                    )
                )
                guard_exists = bool(
                    await connection.scalar(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                  FROM pg_trigger trigger
                                  JOIN pg_class relation ON relation.oid = trigger.tgrelid
                                 WHERE relation.relnamespace = current_schema()::regnamespace
                                   AND relation.relname = 'compensation_cases'
                                   AND trigger.tgname =
                                       'trg_compensation_cases_completed_refund_total'
                            )
                            """
                        )
                    )
                )
                return downgraded_tables, upgraded_tables, guard_exists

        downgraded, upgraded, guard_exists = asyncio.run(migrate_and_inspect())
        compensation_tables = {
            "compensation_cases",
            "compensation_events",
            "payment_refunds",
            "refund_outbox_events",
        }
        assert compensation_tables.isdisjoint(downgraded)
        assert compensation_tables <= upgraded
        assert guard_exists
    finally:
        asyncio.run(isolated.database.dispose())
        asyncio.run(_drop_isolated_schema(isolated.database_url, isolated.schema))


@pytest.mark.parametrize(
    ("state", "provider_status"),
    [
        (PaymentRefundState.REFUNDED, "pending"),
        (PaymentRefundState.REFUND_FAILED, "processed"),
    ],
)
def test_postgresql_direct_write_rejects_incoherent_terminal_provider_evidence(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
    state: PaymentRefundState,
    provider_status: str,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label=f"terminal-provider-evidence-{state.value}-{provider_status}",
    )
    asyncio.run(_direct_transition_case_to_executing(refund_database, fixture))

    async def insert_invalid() -> None:
        refund_id = new_payment_refund_id()
        async with refund_database.database.session() as session:
            session.add(
                PaymentRefund(
                    id=refund_id,
                    compensation_case_id=fixture.case_id,
                    transaction_id=fixture.transaction_id,
                    payment_attempt_id=fixture.payment_attempt_id,
                    provider=PaymentProvider.RAZORPAY,
                    provider_payment_id=fixture.provider_payment_id,
                    provider_refund_id=f"rfnd_{uuid.uuid4().hex}",
                    amount=fixture.amount,
                    currency=fixture.currency,
                    refund_state=state,
                    provider_status=provider_status,
                    provider_receipt=refund_id,
                    created_at=NOW,
                    requested_at=NOW,
                    processed_at=NOW if state is PaymentRefundState.REFUNDED else None,
                    failed_at=NOW if state is PaymentRefundState.REFUND_FAILED else None,
                    last_reconciled_at=None,
                    reconciliation_required_at=None,
                    reconciliation_reason_code=None,
                    revision=1,
                )
            )
            with pytest.raises(IntegrityError, match="terminal_provider_evidence"):
                await session.flush()

    asyncio.run(insert_invalid())


def test_postgresql_completed_case_requires_exact_processed_refund_aggregate(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    insufficient = _create_approved_case(
        refund_client,
        refund_database,
        label="completed-refund-total-insufficient",
    )
    asyncio.run(_direct_transition_case_to_executing(refund_database, insufficient))

    async def insert_processed_refund(
        fixture: ApprovedCaseFixture,
        *,
        amount: int,
    ) -> PaymentRefund:
        async with refund_database.database.session() as session:
            refund, event = _manual_refund(
                fixture,
                amount=amount,
                state=PaymentRefundState.REFUNDED,
                now=NOW + timedelta(seconds=1),
            )
            session.add(refund)
            await session.flush()
            session.add(event)
            await session.commit()
            return refund

    partial = asyncio.run(insert_processed_refund(insufficient, amount=insufficient.amount - 1))

    async def complete_case(
        fixture: ApprovedCaseFixture,
        refund: PaymentRefund,
        *,
        should_succeed: bool,
    ) -> None:
        async with refund_database.database.session() as session:
            await session.execute(
                text(
                    """
                    UPDATE compensation_cases
                       SET decision_state = 'completed', closed_at = :closed_at, revision = 3
                     WHERE id = :case_id
                    """
                ),
                {"case_id": fixture.case_id, "closed_at": NOW + timedelta(seconds=2)},
            )
            session.add(
                CompensationEvent(
                    id=new_compensation_event_id(),
                    compensation_case_id=fixture.case_id,
                    transaction_id=fixture.transaction_id,
                    payment_refund_id=refund.id,
                    sequence=101,
                    case_revision=3,
                    refund_revision=1,
                    event_type=CompensationEventType.COMPENSATION_CLOSED,
                    actor_type=CompensationEventActorType.OPERATOR,
                    actor_id="postgres-direct-write-test",
                    reason_code="COMPENSATION_CLOSED_REFUNDED",
                    event_metadata={"test": "direct-case-completion"},
                    idempotency_key=f"direct-completed:{fixture.case_id}",
                    occurred_at=NOW + timedelta(seconds=2),
                )
            )
            if should_succeed:
                await session.commit()
            else:
                with pytest.raises(
                    IntegrityError,
                    match="lacks its exact processed refund total",
                ):
                    await session.commit()

    asyncio.run(complete_case(insufficient, partial, should_succeed=False))

    exact = _create_approved_case(
        refund_client,
        refund_database,
        label="completed-refund-total-exact",
    )
    asyncio.run(_direct_transition_case_to_executing(refund_database, exact))
    full = asyncio.run(insert_processed_refund(exact, amount=exact.amount))
    asyncio.run(complete_case(exact, full, should_succeed=True))

    async def read_states() -> tuple[CompensationDecisionState, CompensationDecisionState]:
        async with refund_database.database.session() as session:
            insufficient_case = await session.get(CompensationCase, insufficient.case_id)
            exact_case = await session.get(CompensationCase, exact.case_id)
            assert insufficient_case is not None and exact_case is not None
            return insufficient_case.decision_state, exact_case.decision_state

    assert asyncio.run(read_states()) == (
        CompensationDecisionState.EXECUTING,
        CompensationDecisionState.COMPLETED,
    )


def test_postgresql_terminal_overlay_is_orthogonal_audited_and_resolvable(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="terminal-refund-overlay",
    )
    provider = ScriptedRefundProvider(create_status="processed")

    async def create_terminal_refund() -> str:
        async with refund_database.database.session() as session:
            result = await RefundApplicationService(
                session,
                provider,
                _refund_settings(),
                clock=lambda: NOW,
            ).execute_case(fixture.case_id)
            assert result.state is PaymentRefundState.REFUNDED
            return result.refund_id

    refund_id = asyncio.run(create_terminal_refund())

    async def set_and_clear_overlay() -> tuple[PaymentRefundState, int, None]:
        async with refund_database.database.session() as session:
            refund = await session.get(PaymentRefund, refund_id)
            assert refund is not None
            next_sequence = (
                int(
                    await session.scalar(
                        select(func.max(CompensationEvent.sequence)).where(
                            CompensationEvent.compensation_case_id == fixture.case_id
                        )
                    )
                    or 0
                )
                + 1
            )
            await session.execute(
                text(
                    """
                    UPDATE payment_refunds
                       SET reconciliation_required_at = :required_at,
                           reconciliation_reason_code = :reason_code,
                           revision = revision + 1
                     WHERE id = :refund_id
                    """
                ),
                {
                    "refund_id": refund_id,
                    "required_at": NOW + timedelta(seconds=10),
                    "reason_code": "REFUND_TERMINAL_EVIDENCE_CONFLICT",
                },
            )
            session.add(
                CompensationEvent(
                    id=new_compensation_event_id(),
                    compensation_case_id=fixture.case_id,
                    transaction_id=fixture.transaction_id,
                    payment_refund_id=refund_id,
                    sequence=next_sequence,
                    case_revision=3,
                    refund_revision=4,
                    event_type=CompensationEventType.REFUND_RECONCILIATION_REQUIRED,
                    actor_type=CompensationEventActorType.PROVIDER_WEBHOOK,
                    actor_id="evt_terminal_conflict",
                    reason_code="REFUND_TERMINAL_EVIDENCE_CONFLICT",
                    event_metadata={"test": "terminal-overlay-set"},
                    idempotency_key=f"terminal-overlay-set:{refund_id}",
                    occurred_at=NOW + timedelta(seconds=10),
                )
            )
            await session.commit()

        async with refund_database.database.session() as session:
            current = await session.get(PaymentRefund, refund_id)
            assert current is not None
            assert current.reconciliation_reason_code == "REFUND_TERMINAL_EVIDENCE_CONFLICT"
            next_sequence = (
                int(
                    await session.scalar(
                        select(func.max(CompensationEvent.sequence)).where(
                            CompensationEvent.compensation_case_id == fixture.case_id
                        )
                    )
                    or 0
                )
                + 1
            )
            await session.execute(
                text(
                    """
                    UPDATE payment_refunds
                       SET reconciliation_required_at = NULL,
                           reconciliation_reason_code = NULL,
                           last_reconciled_at = :reconciled_at,
                           revision = revision + 1
                     WHERE id = :refund_id
                    """
                ),
                {
                    "refund_id": refund_id,
                    "reconciled_at": NOW + timedelta(seconds=20),
                },
            )
            session.add(
                CompensationEvent(
                    id=new_compensation_event_id(),
                    compensation_case_id=fixture.case_id,
                    transaction_id=fixture.transaction_id,
                    payment_refund_id=refund_id,
                    sequence=next_sequence,
                    case_revision=3,
                    refund_revision=5,
                    event_type=CompensationEventType.REFUND_RECONCILIATION_RESOLVED,
                    actor_type=CompensationEventActorType.PROVIDER_API,
                    actor_id=next(iter(provider.remote_refunds)),
                    reason_code="REFUND_RECONCILIATION_RESOLVED",
                    event_metadata={"test": "terminal-overlay-cleared"},
                    idempotency_key=f"terminal-overlay-cleared:{refund_id}",
                    occurred_at=NOW + timedelta(seconds=20),
                )
            )
            await session.commit()
            await session.refresh(current)
            assert current.last_reconciled_at == NOW + timedelta(seconds=20)
            return current.refund_state, current.revision, current.reconciliation_required_at

    assert asyncio.run(set_and_clear_overlay()) == (PaymentRefundState.REFUNDED, 5, None)


def test_postgresql_direct_write_cannot_rewrite_approved_decision_evidence(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="approved-decision-immutable",
    )

    async def rewrite() -> None:
        async with refund_database.database.session() as session:
            with pytest.raises(DBAPIError, match="provenance and reason are immutable"):
                await session.execute(
                    text(
                        """
                        UPDATE compensation_cases
                           SET decision_state = 'executing',
                               decision_provenance = 'manual_approved',
                               decision_reason_code = 'CHANGED_AFTER_APPROVAL',
                               revision = 2
                         WHERE id = :case_id
                        """
                    ),
                    {"case_id": fixture.case_id},
                )

    asyncio.run(rewrite())


def test_postgresql_partial_refunds_allow_two_plus_three_but_reject_one_more(
    refund_client: TestClient,
    refund_database: IsolatedDatabase,
) -> None:
    fixture = _create_approved_case(
        refund_client,
        refund_database,
        label="refund-partial-aggregate-cap",
    )
    assert fixture.amount == 500

    async def exercise() -> tuple[list[int], Exception | None]:
        async with refund_database.database.session() as session:
            case = await CompensationCaseRepository(session).get_for_update(fixture.case_id)
            assert case is not None
            case.decision_state = CompensationDecisionState.EXECUTING
            case.revision = 2
            session.add(
                CompensationEvent(
                    id=new_compensation_event_id(),
                    compensation_case_id=case.id,
                    transaction_id=case.transaction_id,
                    payment_refund_id=None,
                    sequence=99,
                    case_revision=2,
                    refund_revision=None,
                    event_type=CompensationEventType.COMPENSATION_APPROVED,
                    actor_type=CompensationEventActorType.OPERATOR,
                    actor_id="postgres-partial-refund-test",
                    reason_code="COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED",
                    event_metadata={"test": "partial-refund-sequence"},
                    idempotency_key=f"partial-refund-executing:{case.id}",
                    occurred_at=NOW,
                )
            )
            await session.commit()

            for index, amount in enumerate((200, 300), start=1):
                refund, event = _manual_refund(
                    fixture,
                    amount=amount,
                    state=PaymentRefundState.REFUNDED,
                    now=NOW + timedelta(seconds=index),
                )
                event.sequence = 99 + index
                event.actor_id = "postgres-partial-refund-test"
                session.add(refund)
                await session.flush()
                session.add(event)
                await session.commit()

            extra, extra_event = _manual_refund(
                fixture,
                amount=100,
                state=PaymentRefundState.REFUND_PENDING,
                now=NOW + timedelta(seconds=3),
            )
            extra_event.sequence = 102
            extra_event.actor_id = "postgres-partial-refund-test"
            session.add(extra)
            try:
                await session.flush()
                session.add(extra_event)
                await session.commit()
            except (DBAPIError, IntegrityError) as error:
                await session.rollback()
                extra_error: Exception | None = error
            else:
                extra_error = None

            amounts = list(
                await session.scalars(
                    select(PaymentRefund.amount)
                    .where(PaymentRefund.compensation_case_id == fixture.case_id)
                    .order_by(PaymentRefund.amount)
                )
            )
            return amounts, extra_error

    amounts, extra_error = asyncio.run(exercise())
    assert amounts == [200, 300]
    assert sum(amounts) == 500
    assert extra_error is not None
    assert "refund reservations exceed approved compensation amount" in str(extra_error)
