from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.domain.approval_hashing import calculate_authorization_hash
from app.domain.enums import (
    ApprovalIdentityStatus,
    MerchantStatus,
    PaymentAttemptStatus,
    PaymentTransactionState,
    PolicyDecision,
    PurchaseType,
    ServiceStatus,
)
from app.domain.exceptions import (
    AuthenticationForbiddenError,
    PaymentConflictError,
    PaymentExpiredError,
    PaymentIntegrityError,
    PaymentVerificationError,
)
from app.domain.hashing import calculate_quote_hash, sha256_bytes, sha256_json
from app.domain.policy_hashing import calculate_policy_hash
from app.models import PaymentTransaction
from app.providers import (
    CreateProviderOrder,
    PaymentProviderRejectedError,
    PaymentProviderTimeoutError,
    ProviderOrder,
    ProviderPayment,
    checkout_signature_digest,
)
from app.schemas.payments import CheckoutVerificationCreate, PaymentTransactionCreate
from app.services.payments import PaymentApplicationService

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
ACCOUNT_ID = "acct_00000000000000000000000001"
OTHER_ACCOUNT_ID = "acct_00000000000000000000000002"
IDENTITY_ID = "aid_00000000000000000000000001"
CREDENTIAL_ID = "pkc_00000000000000000000000001"
AUTHORIZATION_ID = "aut_00000000000000000000000001"
EVALUATION_ID = "pye_00000000000000000000000001"
POLICY_ID = "pol_00000000000000000000000001"
QUOTE_ID = "qte_00000000000000000000000001"
MERCHANT_ID = "mrc_00000000000000000000000001"
SERVICE_ID = "svc_00000000000000000000000001"
ORDER_ID = "order_TESTORDER01"
OTHER_ORDER_ID = "order_OTHERORDER02"
PAYMENT_ID = "pay_TESTPAYMENT01"
OTHER_PAYMENT_ID = "pay_OTHERPAYMENT02"
KEY_SECRET = "test-key-secret"
_UNSET = object()


@dataclass
class MutableClock:
    current: datetime = NOW

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int = 1) -> None:
        self.current += timedelta(seconds=seconds)


def payment_settings() -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        redis_url="redis://localhost:6379/15",
        payments_enabled=True,
        razorpay_key_id="rzp_test_12345678",
        razorpay_key_secret=KEY_SECRET,
        razorpay_webhook_secret="test-webhook-secret",
        cors_allowed_origins=["http://localhost:3000"],
        webauthn_expected_origins=["http://localhost:3000"],
        _env_file=None,
    )


def make_provider_order(
    *,
    receipt: str,
    order_id: str = ORDER_ID,
    amount: int = 500,
    currency: str = "INR",
    status: str = "created",
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


def make_provider_payment(
    *,
    status: str,
    payment_id: str = PAYMENT_ID,
    order_id: str = ORDER_ID,
    amount: int = 500,
    currency: str = "INR",
    created_at: datetime = NOW,
) -> ProviderPayment:
    captured = status in {"captured", "refunded"}
    return ProviderPayment(
        id=payment_id,
        order_id=order_id,
        amount=amount,
        currency=currency,
        status=status,  # type: ignore[arg-type]
        captured=captured,
        amount_refunded=amount if status == "refunded" else 0,
        method="upi",
        created_at=created_at,
    )


class FakeProvider:
    def __init__(self) -> None:
        self.create_outcome: (
            ProviderOrder | Exception | Callable[[CreateProviderOrder], ProviderOrder] | object
        ) = _UNSET
        self.find_result: tuple[ProviderOrder, ...] = ()
        self.fetch_order_outcome: ProviderOrder | Exception | object = _UNSET
        self.fetch_payment_outcome: ProviderPayment | Exception | object = _UNSET
        self.fetch_payments_result: tuple[ProviderPayment, ...] | Exception = ()
        self.orders: dict[str, ProviderOrder] = {}
        self.create_calls: list[CreateProviderOrder] = []
        self.find_calls: list[tuple[str, datetime | None, datetime | None]] = []
        self.fetch_order_calls: list[str] = []
        self.fetch_payment_calls: list[str] = []
        self.fetch_payments_calls: list[str] = []

    async def create_order(self, request: CreateProviderOrder) -> ProviderOrder:
        self.create_calls.append(request)
        outcome = self.create_outcome
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            order = outcome(request)
        elif isinstance(outcome, ProviderOrder):
            order = outcome
        else:
            order = make_provider_order(receipt=request.receipt)
        self.orders[order.id] = order
        return order

    async def fetch_order(self, provider_order_id: str) -> ProviderOrder:
        self.fetch_order_calls.append(provider_order_id)
        outcome = self.fetch_order_outcome
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, ProviderOrder):
            return outcome
        return self.orders[provider_order_id]

    async def find_orders_by_receipt(
        self,
        receipt: str,
        *,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> tuple[ProviderOrder, ...]:
        self.find_calls.append((receipt, created_from, created_to))
        return self.find_result

    async def fetch_payments_for_order(
        self,
        provider_order_id: str,
    ) -> tuple[ProviderPayment, ...]:
        self.fetch_payments_calls.append(provider_order_id)
        if isinstance(self.fetch_payments_result, Exception):
            raise self.fetch_payments_result
        return self.fetch_payments_result

    async def fetch_payment(self, provider_payment_id: str) -> ProviderPayment:
        self.fetch_payment_calls.append(provider_payment_id)
        outcome = self.fetch_payment_outcome
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, ProviderPayment):
            return outcome
        raise AssertionError("Fake provider payment outcome was not configured")


@dataclass
class MemoryState:
    transactions: dict[str, PaymentTransaction] = field(default_factory=dict)
    events_by_key: dict[str, Any] = field(default_factory=dict)
    attempts_by_payment_id: dict[str, Any] = field(default_factory=dict)
    webhooks_by_provider_event_id: dict[str, Any] = field(default_factory=dict)
    transaction_locks: dict[str, asyncio.Lock] = field(default_factory=dict)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self._held_locks: list[asyncio.Lock] = []

    async def hold(self, lock: asyncio.Lock) -> None:
        if lock not in self._held_locks:
            await lock.acquire()
            self._held_locks.append(lock)

    async def commit(self) -> None:
        self.commits += 1
        self._release_locks()

    async def rollback(self) -> None:
        self.rollbacks += 1
        self._release_locks()

    def _release_locks(self) -> None:
        for lock in self._held_locks:
            lock.release()
        self._held_locks.clear()


class LookupRepository:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    async def get(self, value_id: str) -> Any | None:
        return self.values.get(value_id)

    async def get_for_update(self, value_id: str) -> Any | None:
        return self.values.get(value_id)


class CatalogRepository:
    def __init__(self, merchant: Any, service: Any) -> None:
        self.merchant = merchant
        self.service = service

    async def get_with_merchant_for_quote(self, service_id: str) -> tuple[Any, Any] | None:
        if service_id != self.service.id:
            return None
        return self.merchant, self.service


def _integrity_error() -> IntegrityError:
    return IntegrityError("duplicate", {}, RuntimeError("unique constraint"))


class MemoryEventRepository:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    async def get_by_idempotency_key(self, key: str) -> Any | None:
        return self.state.events_by_key.get(key)


class MemoryAttemptRepository:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    async def list_for_transaction(self, transaction_id: str) -> list[Any]:
        return [
            attempt
            for attempt in self.state.attempts_by_payment_id.values()
            if attempt.transaction_id == transaction_id
        ]

    async def get_by_provider_payment_id_for_update(self, payment_id: str) -> Any | None:
        return self.state.attempts_by_payment_id.get(payment_id)


class MemoryWebhookRepository:
    def __init__(self, state: MemoryState, session: FakeSession) -> None:
        self.state = state
        self.session = session

    async def get_by_provider_event_id(self, event_id: str) -> Any | None:
        return self.state.webhooks_by_provider_event_id.get(event_id)

    async def create(self, event: Any) -> Any:
        if event.provider_event_id in self.state.webhooks_by_provider_event_id:
            await self.session.rollback()
            raise _integrity_error()
        self.state.webhooks_by_provider_event_id[event.provider_event_id] = event
        await self.session.commit()
        return event


class MemoryTransactionRepository:
    def __init__(
        self,
        state: MemoryState,
        session: FakeSession,
        clock: MutableClock,
    ) -> None:
        self.state = state
        self.session = session
        self.clock = clock

    async def get(self, transaction_id: str) -> PaymentTransaction | None:
        return self.state.transactions.get(transaction_id)

    async def get_for_update(self, transaction_id: str) -> PaymentTransaction | None:
        transaction = self.state.transactions.get(transaction_id)
        if transaction is not None:
            lock = self.state.transaction_locks.setdefault(transaction_id, asyncio.Lock())
            await self.session.hold(lock)
        return transaction

    async def get_by_authorization_id(
        self,
        authorization_id: str,
    ) -> PaymentTransaction | None:
        return next(
            (
                transaction
                for transaction in self.state.transactions.values()
                if transaction.authorization_id == authorization_id
            ),
            None,
        )

    async def get_by_authorization_id_for_update(
        self,
        authorization_id: str,
    ) -> PaymentTransaction | None:
        transaction = await self.get_by_authorization_id(authorization_id)
        if transaction is not None:
            return await self.get_for_update(transaction.id)
        return None

    async def get_by_provider_order_id(self, order_id: str) -> PaymentTransaction | None:
        return next(
            (
                transaction
                for transaction in self.state.transactions.values()
                if transaction.provider_order_id == order_id
            ),
            None,
        )

    async def get_by_provider_receipt(self, receipt: str) -> PaymentTransaction | None:
        return next(
            (
                transaction
                for transaction in self.state.transactions.values()
                if transaction.provider_receipt == receipt
            ),
            None,
        )

    async def create_with_event(self, transaction: PaymentTransaction, *, event: Any) -> Any:
        if await self.get_by_authorization_id(transaction.authorization_id) is not None:
            await self.session.rollback()
            raise _integrity_error()
        transaction.created_at = self.clock.current
        transaction.updated_at = self.clock.current
        self.state.transactions[transaction.id] = transaction
        self.state.events_by_key[event.idempotency_key] = event
        await self.session.commit()
        return transaction

    async def update_with_event(
        self,
        transaction: PaymentTransaction,
        *,
        event: Any,
        attempt: Any | None = None,
        attempts: Sequence[Any] = (),
        webhook_event: Any | None = None,
    ) -> PaymentTransaction:
        if event.idempotency_key in self.state.events_by_key:
            await self.session.rollback()
            raise _integrity_error()
        attempt_records = [*attempts]
        if attempt is not None:
            attempt_records.append(attempt)
        attempt_ids = [record.id for record in attempt_records]
        if len(attempt_ids) != len(set(attempt_ids)):
            await self.session.rollback()
            raise ValueError("duplicate attempt evidence")
        if (
            webhook_event is not None
            and webhook_event.provider_event_id in self.state.webhooks_by_provider_event_id
        ):
            await self.session.rollback()
            raise _integrity_error()

        transaction.revision += 1
        transaction.updated_at = self.clock.current
        self.state.transactions[transaction.id] = transaction
        self.state.events_by_key[event.idempotency_key] = event
        for record in attempt_records:
            self.state.attempts_by_payment_id[record.provider_payment_id] = record
        if webhook_event is not None:
            self.state.webhooks_by_provider_event_id[webhook_event.provider_event_id] = (
                webhook_event
            )
        await self.session.commit()
        return transaction


class FakeEvaluationService:
    def __init__(self, evaluation: Any) -> None:
        self.evaluation = evaluation

    async def get_verified(self, evaluation_id: str) -> Any:
        if evaluation_id != self.evaluation.id:
            raise LookupError(evaluation_id)
        return self.evaluation


@dataclass
class TrustedFixtures:
    identity: Any
    authorization: Any
    policy: Any
    quote: Any
    evaluation: Any
    merchant: Any
    service: Any


def trusted_fixtures() -> TrustedFixtures:
    input_value = {"report": "weekly"}
    input_hash = sha256_json(input_value)
    service_snapshot = {
        "version": "1",
        "json_schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        "merchant": {"id": MERCHANT_ID, "slug": "orbitintel", "name": "OrbitIntel"},
        "service": {
            "id": SERVICE_ID,
            "slug": "weekly-report",
            "name": "Weekly Report",
            "service_type": "report",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "output_content_type": "application/json",
        },
        "pricing": {"amount": 500, "currency": "INR", "purchase_type": "one_time"},
        "fulfillment": {"maximum_seconds": 300, "refund_on_failure": False},
    }
    quote_fields = {
        "quote_id": QUOTE_ID,
        "merchant_id": MERCHANT_ID,
        "service_id": SERVICE_ID,
        "service_snapshot": service_snapshot,
        "input_value": input_value,
        "input_hash": input_hash,
        "amount": 500,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "maximum_fulfillment_seconds": 300,
        "refund_on_fulfillment_failure": False,
        "issued_at": NOW - timedelta(minutes=2),
        "expires_at": NOW + timedelta(minutes=10),
    }
    quote = SimpleNamespace(
        id=QUOTE_ID,
        merchant_id=MERCHANT_ID,
        service_id=SERVICE_ID,
        service_snapshot=service_snapshot,
        input=input_value,
        input_hash=input_hash,
        amount=500,
        currency="INR",
        purchase_type=PurchaseType.ONE_TIME,
        maximum_fulfillment_seconds=300,
        refund_on_fulfillment_failure=False,
        issued_at=quote_fields["issued_at"],
        expires_at=quote_fields["expires_at"],
        quote_hash=calculate_quote_hash(**quote_fields),
    )

    policy_fields = {
        "policy_id": POLICY_ID,
        "subject_ref": ACCOUNT_ID,
        "maximum_amount": 1_000,
        "allowed_currencies": ["INR"],
        "allowed_merchant_ids": [MERCHANT_ID],
        "allowed_service_ids": [SERVICE_ID],
        "allowed_service_types": ["report"],
        "allowed_purchase_types": ["one_time"],
        "issued_at": NOW - timedelta(minutes=3),
        "expires_at": NOW + timedelta(minutes=15),
        "policy_version": "1",
    }
    policy = SimpleNamespace(
        id=POLICY_ID,
        subject_ref=ACCOUNT_ID,
        maximum_amount=1_000,
        allowed_currencies=["INR"],
        allowed_merchant_ids=[MERCHANT_ID],
        allowed_service_ids=[SERVICE_ID],
        allowed_service_types=["report"],
        allowed_purchase_types=["one_time"],
        issued_at=policy_fields["issued_at"],
        expires_at=policy_fields["expires_at"],
        policy_version="1",
        policy_hash=calculate_policy_hash(**policy_fields),
    )
    evaluation = SimpleNamespace(
        id=EVALUATION_ID,
        decision=PolicyDecision.ALLOW,
        policy_id=POLICY_ID,
        quote_id=QUOTE_ID,
        policy_hash=policy.policy_hash,
        quote_hash=quote.quote_hash,
    )
    authorization_fields = {
        "authorization_id": AUTHORIZATION_ID,
        "authorization_version": "1",
        "approval_identity_id": IDENTITY_ID,
        "passkey_credential_id": CREDENTIAL_ID,
        "subject_ref": ACCOUNT_ID,
        "evaluation_id": EVALUATION_ID,
        "policy_id": POLICY_ID,
        "policy_hash": policy.policy_hash,
        "quote_id": QUOTE_ID,
        "quote_hash": quote.quote_hash,
        "merchant_id": MERCHANT_ID,
        "service_id": SERVICE_ID,
        "amount": 500,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "review_hash": sha256_bytes(b"review"),
        "challenge_hash": sha256_bytes(b"challenge"),
        "authorized_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=5),
    }
    authorization = SimpleNamespace(
        id=AUTHORIZATION_ID,
        approval_identity_id=IDENTITY_ID,
        passkey_credential_id=CREDENTIAL_ID,
        evaluation_id=EVALUATION_ID,
        policy_id=POLICY_ID,
        policy_hash=policy.policy_hash,
        quote_id=QUOTE_ID,
        quote_hash=quote.quote_hash,
        merchant_id=MERCHANT_ID,
        service_id=SERVICE_ID,
        subject_ref=ACCOUNT_ID,
        amount=500,
        currency="INR",
        purchase_type=PurchaseType.ONE_TIME,
        review_hash=authorization_fields["review_hash"],
        challenge_hash=authorization_fields["challenge_hash"],
        authorized_at=authorization_fields["authorized_at"],
        expires_at=authorization_fields["expires_at"],
        authorization_version="1",
        authorization_hash=calculate_authorization_hash(**authorization_fields),
    )
    return TrustedFixtures(
        identity=SimpleNamespace(
            id=IDENTITY_ID,
            account_id=ACCOUNT_ID,
            subject_ref=ACCOUNT_ID,
            status=ApprovalIdentityStatus.ACTIVE,
        ),
        authorization=authorization,
        policy=policy,
        quote=quote,
        evaluation=evaluation,
        merchant=SimpleNamespace(id=MERCHANT_ID, status=MerchantStatus.ACTIVE),
        service=SimpleNamespace(
            id=SERVICE_ID,
            merchant_id=MERCHANT_ID,
            status=ServiceStatus.ACTIVE,
        ),
    )


@dataclass
class ServiceHarness:
    fixtures: TrustedFixtures
    provider: FakeProvider
    state: MemoryState
    clock: MutableClock

    def build_service(self) -> PaymentApplicationService:
        session = FakeSession()
        application_service = PaymentApplicationService(
            session,  # type: ignore[arg-type]
            self.provider,
            payment_settings(),
            clock=self.clock,
        )
        application_service._authorizations = LookupRepository(  # noqa: SLF001
            {self.fixtures.authorization.id: self.fixtures.authorization}
        )
        application_service._identities = LookupRepository(  # noqa: SLF001
            {self.fixtures.identity.id: self.fixtures.identity}
        )
        application_service._policies = LookupRepository(  # noqa: SLF001
            {self.fixtures.policy.id: self.fixtures.policy}
        )
        application_service._quotes = LookupRepository(  # noqa: SLF001
            {self.fixtures.quote.id: self.fixtures.quote}
        )
        application_service._services = CatalogRepository(  # noqa: SLF001
            self.fixtures.merchant,
            self.fixtures.service,
        )
        application_service._transactions = MemoryTransactionRepository(  # noqa: SLF001
            self.state,
            session,
            self.clock,
        )
        application_service._attempts = MemoryAttemptRepository(self.state)  # noqa: SLF001
        application_service._events = MemoryEventRepository(self.state)  # noqa: SLF001
        application_service._webhooks = MemoryWebhookRepository(  # noqa: SLF001
            self.state,
            session,
        )
        application_service._evaluation_service = FakeEvaluationService(  # noqa: SLF001
            self.fixtures.evaluation
        )
        return application_service

    async def create_transaction(self) -> Any:
        return await self.build_service().create_transaction(
            PaymentTransactionCreate(authorization_id=AUTHORIZATION_ID),
            account_id=ACCOUNT_ID,
        )

    @property
    def transaction(self) -> PaymentTransaction:
        return next(iter(self.state.transactions.values()))


def make_harness() -> ServiceHarness:
    return ServiceHarness(
        fixtures=trusted_fixtures(),
        provider=FakeProvider(),
        state=MemoryState(),
        clock=MutableClock(),
    )


def checkout_payload(
    transaction: PaymentTransaction,
    *,
    payment_id: str = PAYMENT_ID,
    signed_order_id: str | None = None,
    returned_order_id: str | None = None,
) -> CheckoutVerificationCreate:
    signature = checkout_signature_digest(
        order_id=signed_order_id or transaction.provider_order_id or "",
        payment_id=payment_id,
        key_secret=KEY_SECRET,
    )
    return CheckoutVerificationCreate(
        razorpay_payment_id=payment_id,
        razorpay_order_id=returned_order_id or transaction.provider_order_id,
        razorpay_signature=signature,
    )


@pytest.mark.asyncio
async def test_transaction_derives_active_authorization_terms_and_is_idempotent() -> None:
    harness = make_harness()

    first = await harness.create_transaction()
    second = await harness.create_transaction()

    transaction = harness.transaction
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.response.transaction_id == second.response.transaction_id == transaction.id
    assert first.response.state is PaymentTransactionState.ORDER_CREATED
    assert first.response.amount == harness.fixtures.authorization.amount == 500
    assert first.response.currency == harness.fixtures.authorization.currency == "INR"
    assert transaction.account_id == ACCOUNT_ID
    assert transaction.authorization_hash == harness.fixtures.authorization.authorization_hash
    assert transaction.provider_receipt == transaction.id
    assert len(harness.provider.create_calls) == 1
    request = harness.provider.create_calls[0]
    assert request.amount == 500
    assert request.currency == "INR"
    assert request.receipt == transaction.id
    assert request.partial_payment is False


@pytest.mark.asyncio
async def test_concurrent_transaction_creation_consumes_authorization_once() -> None:
    harness = make_harness()

    first, second = await asyncio.gather(
        harness.build_service().create_transaction(
            PaymentTransactionCreate(authorization_id=AUTHORIZATION_ID),
            account_id=ACCOUNT_ID,
        ),
        harness.build_service().create_transaction(
            PaymentTransactionCreate(authorization_id=AUTHORIZATION_ID),
            account_id=ACCOUNT_ID,
        ),
    )

    assert first.response.transaction_id == second.response.transaction_id
    assert len(harness.state.transactions) == 1
    assert len(harness.provider.create_calls) == 1


@pytest.mark.asyncio
async def test_existing_transaction_resumes_after_authorization_expiry() -> None:
    harness = make_harness()
    first = await harness.create_transaction()
    harness.clock.current = harness.fixtures.authorization.expires_at + timedelta(seconds=1)

    resumed = await harness.create_transaction()

    assert resumed.status_code == 200
    assert resumed.response.transaction_id == first.response.transaction_id
    assert resumed.response.state is PaymentTransactionState.ORDER_CREATED
    assert len(harness.provider.create_calls) == 1


@pytest.mark.asyncio
async def test_wrong_account_cannot_create_or_access_payment_transaction() -> None:
    create_harness = make_harness()
    with pytest.raises(AuthenticationForbiddenError) as create_error:
        await create_harness.build_service().create_transaction(
            PaymentTransactionCreate(authorization_id=AUTHORIZATION_ID),
            account_id=OTHER_ACCOUNT_ID,
        )
    assert create_error.value.reason_code == "AUTH_RESOURCE_OWNERSHIP_MISMATCH"
    assert create_harness.provider.create_calls == []
    assert create_harness.state.transactions == {}

    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()

    with pytest.raises(AuthenticationForbiddenError) as get_error:
        await service.get_transaction(transaction.id, account_id=OTHER_ACCOUNT_ID)
    with pytest.raises(AuthenticationForbiddenError) as verify_error:
        await service.verify_checkout(
            transaction.id,
            checkout_payload(transaction),
            account_id=OTHER_ACCOUNT_ID,
        )
    with pytest.raises(AuthenticationForbiddenError) as reconcile_error:
        await service.reconcile_transaction(transaction.id, account_id=OTHER_ACCOUNT_ID)

    assert get_error.value.reason_code == "AUTH_RESOURCE_OWNERSHIP_MISMATCH"
    assert verify_error.value.reason_code == "AUTH_RESOURCE_OWNERSHIP_MISMATCH"
    assert reconcile_error.value.reason_code == "AUTH_RESOURCE_OWNERSHIP_MISMATCH"
    assert harness.provider.fetch_order_calls == []
    assert harness.provider.fetch_payment_calls == []
    assert harness.provider.fetch_payments_calls == []


@pytest.mark.asyncio
async def test_tampered_authorization_fails_before_provider_order_creation() -> None:
    harness = make_harness()
    harness.fixtures.authorization.amount += 1

    with pytest.raises(PaymentIntegrityError) as error_info:
        await harness.create_transaction()

    assert error_info.value.reason_code == "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED"
    assert harness.provider.create_calls == []
    assert harness.state.transactions == {}


@pytest.mark.asyncio
async def test_deny_evaluation_fails_before_provider_order_creation() -> None:
    harness = make_harness()
    harness.fixtures.evaluation.decision = PolicyDecision.DENY

    with pytest.raises(PaymentIntegrityError) as error_info:
        await harness.create_transaction()

    assert error_info.value.reason_code == "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED"
    assert harness.provider.create_calls == []
    assert harness.state.transactions == {}


@pytest.mark.asyncio
async def test_disabled_identity_and_expired_authorization_fail_before_provider() -> None:
    disabled = make_harness()
    disabled.fixtures.identity.status = ApprovalIdentityStatus.DISABLED
    with pytest.raises(PaymentConflictError) as disabled_info:
        await disabled.create_transaction()
    assert disabled_info.value.reason_code == "PAYMENT_APPROVAL_IDENTITY_DISABLED"
    assert disabled.provider.create_calls == []

    expired = make_harness()
    expired.fixtures.authorization.expires_at = NOW
    expired.fixtures.authorization.authorization_hash = calculate_authorization_hash(
        authorization_id=expired.fixtures.authorization.id,
        authorization_version=expired.fixtures.authorization.authorization_version,
        approval_identity_id=expired.fixtures.authorization.approval_identity_id,
        passkey_credential_id=expired.fixtures.authorization.passkey_credential_id,
        subject_ref=expired.fixtures.authorization.subject_ref,
        evaluation_id=expired.fixtures.authorization.evaluation_id,
        policy_id=expired.fixtures.authorization.policy_id,
        policy_hash=expired.fixtures.authorization.policy_hash,
        quote_id=expired.fixtures.authorization.quote_id,
        quote_hash=expired.fixtures.authorization.quote_hash,
        merchant_id=expired.fixtures.authorization.merchant_id,
        service_id=expired.fixtures.authorization.service_id,
        amount=expired.fixtures.authorization.amount,
        currency=expired.fixtures.authorization.currency,
        purchase_type=expired.fixtures.authorization.purchase_type,
        review_hash=expired.fixtures.authorization.review_hash,
        challenge_hash=expired.fixtures.authorization.challenge_hash,
        authorized_at=expired.fixtures.authorization.authorized_at,
        expires_at=expired.fixtures.authorization.expires_at,
    )
    with pytest.raises(PaymentExpiredError) as expired_info:
        await expired.create_transaction()
    assert expired_info.value.reason_code == "PAYMENT_AUTHORIZATION_EXPIRED"
    assert expired.provider.create_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [
        (
            PaymentProviderRejectedError("create_order", "rejected"),
            PaymentTransactionState.ORDER_CREATION_FAILED,
        ),
        (
            PaymentProviderTimeoutError("create_order", "timeout"),
            PaymentTransactionState.ORDER_CREATION_UNCERTAIN,
        ),
        (
            lambda request: make_provider_order(receipt=request.receipt, amount=501),
            PaymentTransactionState.RECONCILIATION_REQUIRED,
        ),
    ],
)
async def test_provider_create_failures_are_fail_closed(
    outcome: Exception | Callable[[CreateProviderOrder], ProviderOrder],
    expected_state: PaymentTransactionState,
) -> None:
    harness = make_harness()
    harness.provider.create_outcome = outcome

    result = await harness.create_transaction()

    assert result.status_code == 202
    assert result.response.state is expected_state
    assert harness.transaction.provider_order_id is None


@pytest.mark.asyncio
async def test_checkout_hmac_is_bound_to_stored_order_and_signature_alone_is_not_paid() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    service = harness.build_service()

    with pytest.raises(PaymentVerificationError) as error_info:
        await service.verify_checkout(
            transaction.id,
            checkout_payload(transaction, signed_order_id=OTHER_ORDER_ID),
            account_id=ACCOUNT_ID,
        )
    assert error_info.value.reason_code == "PAYMENT_CHECKOUT_SIGNATURE_INVALID"
    assert harness.provider.fetch_payment_calls == []

    harness.provider.fetch_payment_outcome = make_provider_payment(status="authorized")
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.provider_receipt,
        status="attempted",
    )
    result = await service.verify_checkout(
        transaction.id,
        checkout_payload(transaction),
        account_id=ACCOUNT_ID,
    )

    assert result.response.state is PaymentTransactionState.PAYMENT_AUTHORIZED
    assert result.response.paid_at is None
    assert result.response.checkout is None
    assert harness.state.attempts_by_payment_id[PAYMENT_ID].captured is False


@pytest.mark.asyncio
async def test_checkout_rejects_client_returned_order_mismatch_before_provider_fetch() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction

    with pytest.raises(PaymentVerificationError) as error_info:
        await harness.build_service().verify_checkout(
            transaction.id,
            checkout_payload(transaction, returned_order_id=OTHER_ORDER_ID),
            account_id=ACCOUNT_ID,
        )

    assert error_info.value.reason_code == "PAYMENT_CHECKOUT_ORDER_MISMATCH"
    assert harness.provider.fetch_order_calls == []
    assert harness.provider.fetch_payment_calls == []


@pytest.mark.asyncio
async def test_checkout_signature_bound_to_different_payment_id_is_rejected() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    mismatched_signature = checkout_signature_digest(
        order_id=transaction.provider_order_id or "",
        payment_id=OTHER_PAYMENT_ID,
        key_secret=KEY_SECRET,
    )
    payload = CheckoutVerificationCreate(
        razorpay_payment_id=PAYMENT_ID,
        razorpay_order_id=transaction.provider_order_id,
        razorpay_signature=mismatched_signature,
    )

    with pytest.raises(PaymentVerificationError) as error_info:
        await harness.build_service().verify_checkout(
            transaction.id,
            payload,
            account_id=ACCOUNT_ID,
        )

    assert error_info.value.reason_code == "PAYMENT_CHECKOUT_SIGNATURE_INVALID"
    assert harness.provider.fetch_order_calls == []
    assert harness.provider.fetch_payment_calls == []


@pytest.mark.asyncio
async def test_valid_checkout_signature_without_provider_truth_never_marks_paid() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    harness.provider.fetch_payment_outcome = PaymentProviderTimeoutError(
        "fetch_payment",
        "timeout",
    )
    harness.provider.fetch_order_outcome = make_provider_order(receipt=transaction.id)

    result = await harness.build_service().verify_checkout(
        transaction.id,
        checkout_payload(transaction),
        account_id=ACCOUNT_ID,
    )

    assert result.status_code == 202
    assert result.response.state is PaymentTransactionState.RECONCILIATION_REQUIRED
    assert result.response.paid_at is None
    assert harness.state.attempts_by_payment_id == {}
    assert any(
        event.event_type == "checkout_signature_verified"
        for event in harness.state.events_by_key.values()
    )


@pytest.mark.asyncio
async def test_checkout_rejects_provider_payment_identity_mismatch() -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    harness.provider.fetch_payment_outcome = make_provider_payment(
        status="captured",
        payment_id=OTHER_PAYMENT_ID,
    )
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.provider_receipt,
        status="paid",
    )

    result = await harness.build_service().verify_checkout(
        transaction.id,
        checkout_payload(transaction),
        account_id=ACCOUNT_ID,
    )

    assert result.status_code == 202
    assert result.response.state is PaymentTransactionState.RECONCILIATION_REQUIRED
    assert result.response.paid_at is None
    assert harness.state.attempts_by_payment_id == {}
    assert any(
        event.reason_code == "PAYMENT_PROVIDER_RESPONSE_MISMATCH"
        and event.event_metadata["actual_provider_payment_id"] == OTHER_PAYMENT_ID
        for event in harness.state.events_by_key.values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payment_status", "order_status", "expected_state"),
    [
        ("authorized", "attempted", PaymentTransactionState.PAYMENT_AUTHORIZED),
        ("failed", "attempted", PaymentTransactionState.PAYMENT_PENDING),
        ("captured", "paid", PaymentTransactionState.PAID),
    ],
)
async def test_checkout_uses_server_fetched_payment_truth(
    payment_status: str,
    order_status: str,
    expected_state: PaymentTransactionState,
) -> None:
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    harness.provider.fetch_payment_outcome = make_provider_payment(status=payment_status)
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.provider_receipt,
        status=order_status,
    )

    result = await harness.build_service().verify_checkout(
        transaction.id,
        checkout_payload(transaction),
        account_id=ACCOUNT_ID,
    )

    assert result.response.state is expected_state
    assert (result.response.paid_at is not None) is (expected_state is PaymentTransactionState.PAID)
    assert harness.state.attempts_by_payment_id[PAYMENT_ID].provider_status is (
        PaymentAttemptStatus(payment_status)
    )


@pytest.mark.asyncio
async def test_concurrent_identical_checkout_callbacks_share_signature_and_evidence_events() -> (
    None
):
    harness = make_harness()
    await harness.create_transaction()
    transaction = harness.transaction
    harness.provider.fetch_payment_outcome = make_provider_payment(status="authorized")
    harness.provider.fetch_order_outcome = make_provider_order(
        receipt=transaction.provider_receipt,
        status="attempted",
    )
    payload = checkout_payload(transaction)
    first_service = harness.build_service()
    second_service = harness.build_service()

    first, second = await asyncio.gather(
        first_service.verify_checkout(transaction.id, payload, account_id=ACCOUNT_ID),
        second_service.verify_checkout(transaction.id, payload, account_id=ACCOUNT_ID),
    )

    assert first.response.state is PaymentTransactionState.PAYMENT_AUTHORIZED
    assert second.response.state is PaymentTransactionState.PAYMENT_AUTHORIZED
    assert (
        sum(
            event.event_type == "checkout_signature_verified"
            for event in harness.state.events_by_key.values()
        )
        == 1
    )
    assert (
        sum(
            event.event_type == "payment_authorized"
            for event in harness.state.events_by_key.values()
        )
        == 1
    )
