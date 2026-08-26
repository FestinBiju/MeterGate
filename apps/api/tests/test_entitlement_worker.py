"""PostgreSQL integration coverage for paid-entitlement outbox processing."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from alembic import command
from app.application import create_app
from app.core.config import Settings
from app.domain.approval_hashing import AUTHORIZATION_VERSION, calculate_authorization_hash
from app.domain.enums import (
    CommerceAggregateType,
    CommerceOutboxEventType,
    PaymentAttemptStatus,
    PaymentEventActorType,
    PaymentProvider,
    PaymentTransactionEventType,
    PaymentTransactionState,
    PurchaseType,
    RazorpayOrderStatus,
)
from app.domain.hashing import sha256_bytes
from app.domain.ids import (
    new_authorization_id,
    new_commerce_outbox_event_id,
    new_payment_attempt_id,
    new_payment_transaction_event_id,
    new_payment_transaction_id,
)
from app.domain.payment_hashing import (
    PAYMENT_BINDING_VERSION,
    calculate_payment_binding_hash,
)
from app.models import (
    CommerceOutboxEvent,
    Entitlement,
    PaymentAttempt,
    PaymentTransaction,
    PaymentTransactionEvent,
    PurchaseAuthorization,
)
from app.providers import PaymentProviderTimeoutError, ProviderOrder, ProviderPayment
from app.repositories.authorizations import PurchaseAuthorizationRepository
from app.repositories.commerce_outbox_events import CommerceOutboxEventRepository
from app.repositories.passkey_credentials import PasskeyCredentialRepository
from app.repositories.payment_transactions import PaymentTransactionRepository
from app.scripts.backfill_entitlement_outbox import backfill_entitlement_outbox
from app.services.readiness import ReadinessService
from app.workers.entitlements import EntitlementOutboxWorker
from tests.test_postgresql_domain import (
    AuthenticatedAccountContext,
    IsolatedDatabase,
    _drop_isolated_schema,
    _prepare_isolated_database,
    authenticated_domain_client,
    create_authenticated_account,
    create_merchant,
    create_service,
    healthy_probe,
)

API_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class PurchaseEvidence:
    current: AuthenticatedAccountContext
    authorization: PurchaseAuthorization


@dataclass(frozen=True, slots=True)
class PaymentFixture:
    transaction_id: str
    account_id: str
    provider_order_id: str
    provider_payment_id: str | None
    amount: int
    currency: str
    provider_created_at: datetime | None
    outbox_created_at: datetime | None


class ExactPaymentProvider:
    """Return one exact capture, or a deliberate value-release fault."""

    def __init__(
        self,
        payment: PaymentFixture,
        *,
        mode: Literal["exact", "timeout", "mismatch"] = "exact",
    ) -> None:
        if payment.provider_payment_id is None or payment.provider_created_at is None:
            raise ValueError("A paid fixture is required")
        self.payment = payment
        self.mode = mode
        self.order_calls = 0
        self.payments_calls = 0

    async def fetch_order(self, provider_order_id: str) -> ProviderOrder:
        self.order_calls += 1
        assert provider_order_id == self.payment.provider_order_id
        if self.mode == "timeout":
            raise PaymentProviderTimeoutError("fetch_order", "temporary timeout")
        amount = self.payment.amount + (1 if self.mode == "mismatch" else 0)
        return ProviderOrder(
            id=self.payment.provider_order_id,
            amount=amount,
            amount_paid=amount,
            amount_due=0,
            currency=self.payment.currency,
            receipt=self.payment.transaction_id,
            status="paid",
            created_at=self.payment.provider_created_at,
        )

    async def fetch_payments_for_order(
        self,
        provider_order_id: str,
    ) -> tuple[ProviderPayment, ...]:
        self.payments_calls += 1
        assert provider_order_id == self.payment.provider_order_id
        assert self.payment.provider_payment_id is not None
        assert self.payment.provider_created_at is not None
        return (
            ProviderPayment(
                id=self.payment.provider_payment_id,
                order_id=self.payment.provider_order_id,
                amount=self.payment.amount,
                currency=self.payment.currency,
                status="captured",
                captured=True,
                amount_refunded=0,
                method="upi",
                created_at=self.payment.provider_created_at,
            ),
        )


class ProviderMustNotBeCalled:
    async def fetch_order(self, provider_order_id: str) -> ProviderOrder:
        raise AssertionError(f"provider called for blocked transaction {provider_order_id}")

    async def fetch_payments_for_order(
        self,
        provider_order_id: str,
    ) -> tuple[ProviderPayment, ...]:
        raise AssertionError(f"provider called for blocked transaction {provider_order_id}")


@pytest.fixture(scope="module")
def worker_database() -> Iterator[IsolatedDatabase]:
    isolated = asyncio.run(_prepare_isolated_database())
    yield isolated
    asyncio.run(isolated.database.dispose())
    asyncio.run(_drop_isolated_schema(isolated.database_url, isolated.schema))


@pytest.fixture(scope="module")
def worker_client(worker_database: IsolatedDatabase) -> Iterator[TestClient]:
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
            database=worker_database.database,
        )
    ) as client:
        yield client


def _worker_settings(*, retry_seconds: int = 5) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        redis_url="redis://localhost:6379/15",
        payments_enabled=True,
        razorpay_key_id="rzp_test_12345678",
        razorpay_key_secret="worker-test-key-secret",
        razorpay_webhook_secret="worker-test-webhook-secret",
        fulfillment_enabled=True,
        entitlement_token_secret="worker-entitlement-token-secret-32-bytes",
        orbitintel_shared_secret="worker-orbitintel-shared-secret-32-bytes",
        entitlement_outbox_retry_base_seconds=retry_seconds,
        entitlement_outbox_retry_max_seconds=retry_seconds,
        cors_allowed_origins=["http://localhost:3000"],
        webauthn_expected_origins=["http://localhost:3000"],
        _env_file=None,
    )


def _create_purchase_evidence(
    client: TestClient,
    isolated: IsolatedDatabase,
    *,
    label: str,
) -> PurchaseEvidence:
    unique = uuid.uuid4().hex
    slug_label = label.replace("_", "-")
    raw_credential_id = f"{label}-credential-{unique}".encode()
    current, credential = asyncio.run(
        create_authenticated_account(
            isolated,
            display_name=f"{label} buyer",
            raw_credential_id=raw_credential_id,
        )
    )
    merchant = create_merchant(client, f"{slug_label}-merchant-{unique[:10]}")
    service = create_service(
        client,
        merchant["id"],
        f"{slug_label}-service-{unique[:10]}",
        base_price=500,
    )
    quote_response = client.post(
        "/api/v1/quotes",
        json={"service_id": service["id"], "input": {"norad_id": 25544}},
    )
    assert quote_response.status_code == 201, quote_response.text
    quote = quote_response.json()
    with authenticated_domain_client(client, current) as authenticated_client:
        policy_response = authenticated_client.post(
            "/api/v1/policies",
            json={
                "maximum_amount": 1_000,
                "allowed_currencies": ["INR"],
                "allowed_merchant_ids": [merchant["id"]],
                "allowed_service_ids": [service["id"]],
                "allowed_service_types": ["report"],
                "allowed_purchase_types": ["one_time"],
                "expires_in_seconds": 900,
            },
        )
        assert policy_response.status_code == 201, policy_response.text
        policy = policy_response.json()
        evaluation_response = authenticated_client.post(
            "/api/v1/policy-evaluations",
            json={"policy_id": policy["id"], "quote_id": quote["id"]},
        )
        assert evaluation_response.status_code == 201, evaluation_response.text
        evaluation = evaluation_response.json()
    assert evaluation["decision"] == "allow"

    authorized_at = datetime.now(UTC)
    expires_at = authorized_at + timedelta(minutes=2)
    authorization_id = new_authorization_id()
    review_hash = sha256_bytes(f"{label}-review-{unique}".encode())
    challenge_hash = sha256_bytes(f"{label}-challenge-{unique}".encode())
    authorization_hash = calculate_authorization_hash(
        authorization_id=authorization_id,
        authorization_version=AUTHORIZATION_VERSION,
        approval_identity_id=current.approval_identity.id,
        passkey_credential_id=credential.id,
        subject_ref=current.account.id,
        evaluation_id=evaluation["id"],
        policy_id=policy["id"],
        policy_hash=policy["policy_hash"],
        quote_id=quote["id"],
        quote_hash=quote["quote_hash"],
        merchant_id=merchant["id"],
        service_id=service["id"],
        amount=quote["pricing"]["amount"],
        currency=quote["pricing"]["currency"],
        purchase_type=PurchaseType(quote["pricing"]["purchase_type"]),
        review_hash=review_hash,
        challenge_hash=challenge_hash,
        authorized_at=authorized_at,
        expires_at=expires_at,
    )
    authorization = PurchaseAuthorization(
        id=authorization_id,
        approval_identity_id=current.approval_identity.id,
        passkey_credential_id=credential.id,
        evaluation_id=evaluation["id"],
        policy_id=policy["id"],
        policy_hash=policy["policy_hash"],
        quote_id=quote["id"],
        quote_hash=quote["quote_hash"],
        merchant_id=merchant["id"],
        service_id=service["id"],
        subject_ref=current.account.id,
        amount=quote["pricing"]["amount"],
        currency=quote["pricing"]["currency"],
        purchase_type=PurchaseType(quote["pricing"]["purchase_type"]),
        review_hash=review_hash,
        challenge_hash=challenge_hash,
        authorized_at=authorized_at,
        expires_at=expires_at,
        authorization_version=AUTHORIZATION_VERSION,
        authorization_hash=authorization_hash,
    )

    async def persist_authorization() -> None:
        async with isolated.database.session() as session:
            stored_credential = await PasskeyCredentialRepository(
                session
            ).get_by_credential_id_for_update(
                raw_credential_id,
                identity_id=current.approval_identity.id,
            )
            assert stored_credential is not None
            await PurchaseAuthorizationRepository(session).create_with_locked_credential_update(
                authorization,
                credential=stored_credential,
                new_sign_count=1,
                last_used_at=authorized_at,
            )

    asyncio.run(persist_authorization())
    return PurchaseEvidence(current=current, authorization=authorization)


def _new_payment_transaction(
    evidence: PurchaseEvidence,
    *,
    transaction_id: str,
    started_at: datetime,
    state: PaymentTransactionState = PaymentTransactionState.ORDER_CREATION_PENDING,
    provider_order_id: str | None = None,
) -> PaymentTransaction:
    authorization = evidence.authorization
    binding = {
        "transaction_id": transaction_id,
        "payment_binding_version": PAYMENT_BINDING_VERSION,
        "account_id": evidence.current.account.id,
        "authorization_id": authorization.id,
        "authorization_hash": authorization.authorization_hash,
        "evaluation_id": authorization.evaluation_id,
        "policy_id": authorization.policy_id,
        "policy_hash": authorization.policy_hash,
        "quote_id": authorization.quote_id,
        "quote_hash": authorization.quote_hash,
        "merchant_id": authorization.merchant_id,
        "service_id": authorization.service_id,
        "amount": authorization.amount,
        "currency": authorization.currency,
        "purchase_type": authorization.purchase_type,
        "provider": PaymentProvider.RAZORPAY,
        "provider_receipt": transaction_id,
    }
    paid = state is PaymentTransactionState.PAID
    has_order = provider_order_id is not None
    return PaymentTransaction(
        id=transaction_id,
        account_id=evidence.current.account.id,
        authorization_id=authorization.id,
        authorization_hash=authorization.authorization_hash,
        evaluation_id=authorization.evaluation_id,
        policy_id=authorization.policy_id,
        policy_hash=authorization.policy_hash,
        quote_id=authorization.quote_id,
        quote_hash=authorization.quote_hash,
        merchant_id=authorization.merchant_id,
        service_id=authorization.service_id,
        amount=authorization.amount,
        currency=authorization.currency,
        purchase_type=authorization.purchase_type,
        provider=PaymentProvider.RAZORPAY,
        provider_receipt=transaction_id,
        provider_order_id=provider_order_id,
        provider_order_status=RazorpayOrderStatus.PAID if paid else None,
        transaction_state=state,
        order_creation_attempts=1,
        order_creation_started_at=started_at,
        order_created_at=started_at if has_order else None,
        paid_at=started_at if paid else None,
        last_reconciled_at=started_at if paid else None,
        payment_binding_version=PAYMENT_BINDING_VERSION,
        payment_binding_hash=calculate_payment_binding_hash(**binding),
        revision=1,
    )


async def _persist_payment(
    isolated: IsolatedDatabase,
    evidence: PurchaseEvidence,
    *,
    final_state: Literal["paid", "order_created", "reconciliation_required"],
) -> PaymentFixture:
    started_at = datetime.now(UTC)
    transaction_id = new_payment_transaction_id()
    order_id = f"order_{uuid.uuid4().hex}"
    transaction = _new_payment_transaction(
        evidence,
        transaction_id=transaction_id,
        started_at=started_at,
    )
    initial_event = PaymentTransactionEvent(
        id=new_payment_transaction_event_id(),
        transaction_id=transaction.id,
        transaction_revision=1,
        event_type=PaymentTransactionEventType.PAYMENT_TRANSACTION_CREATED,
        actor_type=PaymentEventActorType.ACCOUNT,
        actor_id=evidence.current.account.id,
        prior_state=None,
        resulting_state=PaymentTransactionState.ORDER_CREATION_PENDING,
        reason_code="PAYMENT_TRANSACTION_CREATED",
        event_metadata={"authorization_id": evidence.authorization.id},
        idempotency_key=f"{transaction.id}:created",
        occurred_at=started_at,
    )
    async with isolated.database.session() as session:
        await PaymentTransactionRepository(session).create_with_event(
            transaction,
            event=initial_event,
        )

    order_created_at = started_at + timedelta(seconds=1)
    async with isolated.database.session() as session:
        repository = PaymentTransactionRepository(session)
        locked = await repository.get_for_update(transaction.id)
        assert locked is not None
        locked.provider_order_id = order_id
        locked.provider_order_status = RazorpayOrderStatus.CREATED
        locked.transaction_state = PaymentTransactionState.ORDER_CREATED
        locked.order_created_at = order_created_at
        await repository.update_with_event(
            locked,
            event=PaymentTransactionEvent(
                id=new_payment_transaction_event_id(),
                transaction_id=locked.id,
                transaction_revision=2,
                event_type=PaymentTransactionEventType.RAZORPAY_ORDER_CREATED,
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=order_id,
                prior_state=PaymentTransactionState.ORDER_CREATION_PENDING,
                resulting_state=PaymentTransactionState.ORDER_CREATED,
                reason_code="PAYMENT_ORDER_CREATED",
                event_metadata={"provider_order_id": order_id},
                idempotency_key=f"{locked.id}:order",
                occurred_at=order_created_at,
            ),
        )

    if final_state == "order_created":
        return PaymentFixture(
            transaction_id,
            evidence.current.account.id,
            order_id,
            None,
            evidence.authorization.amount,
            evidence.authorization.currency,
            None,
            None,
        )

    observed_at = started_at + timedelta(seconds=2)
    if final_state == "reconciliation_required":
        async with isolated.database.session() as session:
            repository = PaymentTransactionRepository(session)
            locked = await repository.get_for_update(transaction.id)
            assert locked is not None
            locked.transaction_state = PaymentTransactionState.RECONCILIATION_REQUIRED
            locked.last_reconciled_at = observed_at
            await repository.update_with_event(
                locked,
                event=PaymentTransactionEvent(
                    id=new_payment_transaction_event_id(),
                    transaction_id=locked.id,
                    transaction_revision=3,
                    event_type=PaymentTransactionEventType.RECONCILIATION_REQUIRED,
                    actor_type=PaymentEventActorType.PROVIDER_API,
                    actor_id=order_id,
                    prior_state=PaymentTransactionState.ORDER_CREATED,
                    resulting_state=PaymentTransactionState.RECONCILIATION_REQUIRED,
                    reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                    event_metadata={},
                    idempotency_key=f"{locked.id}:reconciliation",
                    occurred_at=observed_at,
                ),
            )
        return PaymentFixture(
            transaction_id,
            evidence.current.account.id,
            order_id,
            None,
            evidence.authorization.amount,
            evidence.authorization.currency,
            None,
            None,
        )

    payment_id = f"pay_{uuid.uuid4().hex}"
    async with isolated.database.session() as session:
        repository = PaymentTransactionRepository(session)
        locked = await repository.get_for_update(transaction.id)
        assert locked is not None
        attempt = PaymentAttempt(
            id=new_payment_attempt_id(),
            transaction_id=locked.id,
            provider=PaymentProvider.RAZORPAY,
            provider_order_id=order_id,
            provider_payment_id=payment_id,
            amount=locked.amount,
            currency=locked.currency,
            provider_status=PaymentAttemptStatus.CAPTURED,
            method="upi",
            captured=True,
            provider_created_at=observed_at,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
        locked.provider_order_status = RazorpayOrderStatus.PAID
        locked.transaction_state = PaymentTransactionState.PAID
        locked.paid_at = observed_at
        locked.last_reconciled_at = observed_at
        await repository.update_with_event(
            locked,
            event=PaymentTransactionEvent(
                id=new_payment_transaction_event_id(),
                transaction_id=locked.id,
                transaction_revision=3,
                event_type=PaymentTransactionEventType.PAYMENT_CAPTURED,
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=payment_id,
                prior_state=PaymentTransactionState.ORDER_CREATED,
                resulting_state=PaymentTransactionState.PAID,
                reason_code="PAYMENT_CAPTURED",
                event_metadata={"provider_payment_id": payment_id},
                payment_attempt_id=attempt.id,
                idempotency_key=f"{locked.id}:captured",
                occurred_at=observed_at,
            ),
            attempt=attempt,
        )
    return PaymentFixture(
        transaction_id,
        evidence.current.account.id,
        order_id,
        payment_id,
        evidence.authorization.amount,
        evidence.authorization.currency,
        observed_at,
        observed_at,
    )


async def _enqueue_blocked_payment(
    isolated: IsolatedDatabase,
    payment: PaymentFixture,
    *,
    available_at: datetime,
) -> None:
    async with isolated.database.session() as session:
        transaction = await session.get(PaymentTransaction, payment.transaction_id)
        assert transaction is not None
        await CommerceOutboxEventRepository(session).create(
            CommerceOutboxEvent(
                id=new_commerce_outbox_event_id(),
                event_type=CommerceOutboxEventType.ENTITLEMENT_ISSUANCE_REQUESTED,
                aggregate_type=CommerceAggregateType.PAYMENT_TRANSACTION,
                aggregate_id=payment.transaction_id,
                deduplication_key=f"entitlement:{payment.transaction_id}",
                payload_version="1",
                payload={
                    "transaction_id": payment.transaction_id,
                    "payment_binding_hash": transaction.payment_binding_hash,
                },
                created_at=available_at,
                available_at=available_at,
                processing_started_at=None,
                processed_at=None,
                attempt_count=0,
                lease_generation=0,
                lease_expires_at=None,
                last_error_code=None,
            )
        )


async def _defer_existing_work(isolated: IsolatedDatabase) -> None:
    async with isolated.database.session() as session:
        pending = await session.scalars(
            select(CommerceOutboxEvent).where(CommerceOutboxEvent.processed_at.is_(None))
        )
        for event in pending:
            event.available_at = max(event.created_at, datetime(2099, 1, 1, tzinfo=UTC))
            event.processing_started_at = None
            event.lease_expires_at = None
        await session.commit()


def test_paid_outbox_worker_issues_exactly_one_entitlement_and_duplicate_poll_is_noop(
    worker_client: TestClient,
    worker_database: IsolatedDatabase,
) -> None:
    asyncio.run(_defer_existing_work(worker_database))
    evidence = _create_purchase_evidence(
        worker_client,
        worker_database,
        label="worker-success",
    )
    payment = asyncio.run(_persist_payment(worker_database, evidence, final_state="paid"))
    assert payment.outbox_created_at is not None
    now = payment.outbox_created_at + timedelta(seconds=1)
    provider = ExactPaymentProvider(payment)
    worker = EntitlementOutboxWorker(
        worker_database.database,
        _worker_settings(),
        provider,
        clock=lambda: now,
    )

    first = asyncio.run(worker.run_once())
    duplicate_poll = asyncio.run(worker.run_once())

    assert (first.claimed, first.processed, first.reason_code) == (True, True, None)
    assert (duplicate_poll.claimed, duplicate_poll.processed) == (False, False)
    assert provider.order_calls == 1
    assert provider.payments_calls == 1

    async def read_state() -> tuple[int, int, bool, int]:
        async with worker_database.database.session() as session:
            entitlement_count = await session.scalar(
                select(func.count(Entitlement.id)).where(
                    Entitlement.transaction_id == payment.transaction_id
                )
            )
            outbox_count = await session.scalar(
                select(func.count(CommerceOutboxEvent.id)).where(
                    CommerceOutboxEvent.aggregate_id == payment.transaction_id
                )
            )
            work = await CommerceOutboxEventRepository(session).get_by_deduplication_key(
                f"entitlement:{payment.transaction_id}"
            )
            assert work is not None
            return (
                entitlement_count or 0,
                outbox_count or 0,
                work.processed_at is not None,
                work.attempt_count,
            )

    assert asyncio.run(read_state()) == (1, 1, True, 1)


@pytest.mark.parametrize(
    ("final_state", "expected_reason"),
    [
        ("order_created", "ENTITLEMENT_TRANSACTION_NOT_PAID"),
        ("reconciliation_required", "ENTITLEMENT_RECONCILIATION_REQUIRED"),
    ],
)
def test_worker_blocks_non_paid_and_reconciliation_transactions_before_provider_io(
    worker_client: TestClient,
    worker_database: IsolatedDatabase,
    final_state: Literal["order_created", "reconciliation_required"],
    expected_reason: str,
) -> None:
    asyncio.run(_defer_existing_work(worker_database))
    evidence = _create_purchase_evidence(
        worker_client,
        worker_database,
        label=f"worker-blocked-{final_state}",
    )
    payment = asyncio.run(_persist_payment(worker_database, evidence, final_state=final_state))
    available_at = datetime.now(UTC) + timedelta(seconds=1)
    asyncio.run(
        _enqueue_blocked_payment(
            worker_database,
            payment,
            available_at=available_at,
        )
    )
    worker = EntitlementOutboxWorker(
        worker_database.database,
        _worker_settings(retry_seconds=300),
        ProviderMustNotBeCalled(),
        clock=lambda: available_at,
    )

    result = asyncio.run(worker.run_once())

    assert (result.claimed, result.processed, result.reason_code) == (
        True,
        False,
        expected_reason,
    )

    async def read_state() -> tuple[int, bool, str | None, bool]:
        async with worker_database.database.session() as session:
            work = await CommerceOutboxEventRepository(session).get_by_deduplication_key(
                f"entitlement:{payment.transaction_id}"
            )
            assert work is not None
            entitlement_count = await session.scalar(
                select(func.count(Entitlement.id)).where(
                    Entitlement.transaction_id == payment.transaction_id
                )
            )
            return (
                work.attempt_count,
                work.processed_at is not None,
                work.last_error_code,
                bool(entitlement_count),
            )

    assert asyncio.run(read_state()) == (1, False, expected_reason, False)


def test_provider_unavailable_then_mismatch_retains_outbox_for_retry(
    worker_client: TestClient,
    worker_database: IsolatedDatabase,
) -> None:
    asyncio.run(_defer_existing_work(worker_database))
    evidence = _create_purchase_evidence(
        worker_client,
        worker_database,
        label="worker-provider-retry",
    )
    payment = asyncio.run(_persist_payment(worker_database, evidence, final_state="paid"))
    assert payment.outbox_created_at is not None
    first_now = payment.outbox_created_at + timedelta(seconds=1)
    provider = ExactPaymentProvider(payment, mode="timeout")
    worker = EntitlementOutboxWorker(
        worker_database.database,
        _worker_settings(retry_seconds=5),
        provider,
        clock=lambda: first_now,
    )

    unavailable = asyncio.run(worker.run_once())

    assert unavailable.reason_code == "ENTITLEMENT_PAYMENT_REVERIFICATION_FAILED"

    async def retry_state() -> tuple[datetime, int, str | None, bool]:
        async with worker_database.database.session() as session:
            work = await CommerceOutboxEventRepository(session).get_by_deduplication_key(
                f"entitlement:{payment.transaction_id}"
            )
            assert work is not None
            return (
                work.available_at,
                work.attempt_count,
                work.last_error_code,
                work.processed_at is not None,
            )

    available_at, attempts, error_code, processed = asyncio.run(retry_state())
    assert available_at == first_now + timedelta(seconds=5)
    assert (attempts, error_code, processed) == (
        1,
        "ENTITLEMENT_PAYMENT_REVERIFICATION_FAILED",
        False,
    )

    provider.mode = "mismatch"
    retry_worker = EntitlementOutboxWorker(
        worker_database.database,
        _worker_settings(retry_seconds=5),
        provider,
        clock=lambda: available_at,
    )
    mismatch = asyncio.run(retry_worker.run_once())

    assert (mismatch.claimed, mismatch.processed, mismatch.reason_code) == (
        True,
        False,
        "ENTITLEMENT_RECONCILIATION_REQUIRED",
    )

    async def final_state() -> tuple[int, str | None, bool, int, int, PaymentTransactionState]:
        async with worker_database.database.session() as session:
            work = await CommerceOutboxEventRepository(session).get_by_deduplication_key(
                f"entitlement:{payment.transaction_id}"
            )
            transaction = await session.get(PaymentTransaction, payment.transaction_id)
            assert work is not None
            assert transaction is not None
            entitlement_count = await session.scalar(
                select(func.count(Entitlement.id)).where(
                    Entitlement.transaction_id == payment.transaction_id
                )
            )
            anomaly_count = await session.scalar(
                select(func.count(PaymentTransactionEvent.id)).where(
                    PaymentTransactionEvent.transaction_id == payment.transaction_id,
                    PaymentTransactionEvent.event_type
                    == PaymentTransactionEventType.RECONCILIATION_REQUIRED,
                )
            )
            return (
                work.attempt_count,
                work.last_error_code,
                work.processed_at is not None,
                entitlement_count or 0,
                anomaly_count or 0,
                PaymentTransactionState(transaction.transaction_state),
            )

    assert asyncio.run(final_state()) == (
        2,
        "ENTITLEMENT_RECONCILIATION_REQUIRED",
        False,
        0,
        1,
        PaymentTransactionState.PAID,
    )


def test_stale_worker_cannot_release_a_successor_lease(
    worker_client: TestClient,
    worker_database: IsolatedDatabase,
) -> None:
    asyncio.run(_defer_existing_work(worker_database))
    evidence = _create_purchase_evidence(
        worker_client,
        worker_database,
        label="worker-lease-fence",
    )
    payment = asyncio.run(_persist_payment(worker_database, evidence, final_state="paid"))
    assert payment.outbox_created_at is not None
    first_claimed_at = payment.outbox_created_at + timedelta(seconds=1)
    successor_claimed_at = first_claimed_at + timedelta(seconds=2)
    successor_lease_expires_at = successor_claimed_at + timedelta(seconds=30)

    async def exercise_race() -> None:
        async with worker_database.database.session() as first_session:
            first_repository = CommerceOutboxEventRepository(first_session)
            stale = await first_repository.claim_next(
                now=first_claimed_at,
                lease_expires_at=first_claimed_at + timedelta(seconds=1),
            )
            assert stale is not None
            stale_id = stale.id
            stale_generation = stale.lease_generation

            async with worker_database.database.session() as successor_session:
                successor = await CommerceOutboxEventRepository(successor_session).claim_next(
                    now=successor_claimed_at,
                    lease_expires_at=successor_lease_expires_at,
                )
                assert successor is not None
                assert successor.id == stale_id
                assert successor.lease_generation == stale_generation + 1

            with pytest.raises(ValueError, match="lease is stale"):
                await first_repository.release_for_retry(
                    stale,
                    expected_lease_generation=stale_generation,
                    available_at=successor_claimed_at + timedelta(seconds=5),
                    error_code="ENTITLEMENT_PAYMENT_REVERIFICATION_FAILED",
                )

        async with worker_database.database.session() as verification_session:
            current = await CommerceOutboxEventRepository(verification_session).get(stale_id)
            assert current is not None
            assert current.lease_generation == stale_generation + 1
            assert current.processing_started_at == successor_claimed_at
            assert current.lease_expires_at == successor_lease_expires_at
            assert current.last_error_code is None

    asyncio.run(exercise_race())


def test_historical_paid_backfill_inserts_once_and_is_idempotent() -> None:
    isolated = asyncio.run(_prepare_isolated_database())
    try:

        async def downgrade_to_payment_gate() -> None:
            async with isolated.database.engine.begin() as connection:

                def downgrade(sync_connection: Any) -> None:
                    config = Config(API_ROOT / "alembic.ini")
                    config.attributes["connection"] = sync_connection
                    command.downgrade(config, "20260825_0006")

                await connection.run_sync(downgrade)

        asyncio.run(downgrade_to_payment_gate())
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
                database=isolated.database,
            )
        ) as client:
            evidence = _create_purchase_evidence(
                client,
                isolated,
                label="historical-paid",
            )

        paid_at = datetime.now(UTC)
        transaction_id = new_payment_transaction_id()
        order_id = f"order_{uuid.uuid4().hex}"
        transaction = _new_payment_transaction(
            evidence,
            transaction_id=transaction_id,
            started_at=paid_at,
            state=PaymentTransactionState.PAID,
            provider_order_id=order_id,
        )
        event = PaymentTransactionEvent(
            id=new_payment_transaction_event_id(),
            transaction_id=transaction.id,
            transaction_revision=1,
            event_type=PaymentTransactionEventType.PAYMENT_CAPTURED,
            actor_type=PaymentEventActorType.PROVIDER_API,
            actor_id=order_id,
            prior_state=None,
            resulting_state=PaymentTransactionState.PAID,
            reason_code="PAYMENT_CAPTURED",
            event_metadata={"historical_fixture": True},
            idempotency_key=f"{transaction.id}:historical-capture",
            occurred_at=paid_at,
        )

        async def persist_historical_payment() -> None:
            async with isolated.database.session() as session:
                await PaymentTransactionRepository(session).create_with_event(
                    transaction,
                    event=event,
                )

        asyncio.run(persist_historical_payment())

        async def upgrade_to_head() -> None:
            async with isolated.database.engine.begin() as connection:

                def upgrade(sync_connection: Any) -> None:
                    config = Config(API_ROOT / "alembic.ini")
                    config.attributes["connection"] = sync_connection
                    command.upgrade(config, "head")

                await connection.run_sync(upgrade)

        asyncio.run(upgrade_to_head())

        first = asyncio.run(backfill_entitlement_outbox(isolated.database))
        second = asyncio.run(backfill_entitlement_outbox(isolated.database))

        assert (first, second) == (1, 0)

        async def read_backfill() -> tuple[int, dict[str, Any]]:
            async with isolated.database.session() as session:
                rows = list(
                    await session.scalars(
                        select(CommerceOutboxEvent).where(
                            CommerceOutboxEvent.aggregate_id == transaction.id
                        )
                    )
                )
                assert len(rows) == 1
                return len(rows), rows[0].payload

        assert asyncio.run(read_backfill()) == (
            1,
            {
                "transaction_id": transaction.id,
                "payment_binding_hash": transaction.payment_binding_hash,
            },
        )
    finally:
        asyncio.run(isolated.database.dispose())
        asyncio.run(_drop_isolated_schema(isolated.database_url, isolated.schema))
