"""Paid-entitlement issuance, lookup, integrity, and capability orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entitlement_hashing import (
    ENTITLEMENT_HASH_PAYLOAD_VERSION,
    calculate_entitlement_hash,
    recompute_entitlement_integrity,
)
from app.domain.enums import (
    FulfillmentEventActorType,
    FulfillmentEventType,
    FulfillmentExecutionState,
    PaymentTransactionState,
)
from app.domain.exceptions import (
    EntitlementConflictError,
    EntitlementExpiredError,
    EntitlementForbiddenError,
    EntitlementIntegrityError,
    EntitlementNotFoundError,
)
from app.domain.ids import new_entitlement_id, new_fulfillment_event_id
from app.domain.integrity import IntegrityStructureError
from app.domain.quote_integrity import verify_quote_integrity
from app.models import (
    CommerceOutboxEvent,
    Entitlement,
    FulfillmentEvent,
    Merchant,
    Service,
)
from app.repositories import (
    CommerceOutboxEventRepository,
    EntitlementRepository,
    FulfillmentEventRepository,
    FulfillmentExecutionRepository,
    MerchantRepository,
    PaymentTransactionEventRepository,
    PaymentTransactionRepository,
    QuoteRepository,
    ServiceRepository,
)
from app.services.capabilities import CapabilityTokenService, IssuedCapability
from app.services.payments import PaymentApplicationService, PaymentValueReleaseProof

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class EntitlementLookup:
    transaction_id: str
    state: Literal["pending", "ready", "blocked"]
    reason_code: str
    entitlement: Entitlement | None


@dataclass(frozen=True, slots=True)
class EntitlementView:
    entitlement: Entitlement
    merchant: Merchant
    service: Service


@dataclass(frozen=True, slots=True)
class EntitlementTimelineEntry:
    event_type: str
    reason_code: str
    occurred_at: datetime


class EntitlementApplicationService:
    """Issue immutable access evidence only from a fresh payment proof."""

    def __init__(
        self,
        session: AsyncSession,
        payment_service: PaymentApplicationService,
        capability_service: CapabilityTokenService,
        *,
        entitlement_ttl: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        if not timedelta(minutes=1) <= entitlement_ttl <= timedelta(days=1):
            raise ValueError("Entitlement lifetime must be between 60 and 86400 seconds")
        self._session = session
        self._payment_service = payment_service
        self._capabilities = capability_service
        self._entitlement_ttl = entitlement_ttl
        self._clock = clock
        self._transactions = PaymentTransactionRepository(session)
        self._outbox = CommerceOutboxEventRepository(session)
        self._entitlements = EntitlementRepository(session)
        self._executions = FulfillmentExecutionRepository(session)
        self._fulfillment_events = FulfillmentEventRepository(session)
        self._payment_events = PaymentTransactionEventRepository(session)
        self._quotes = QuoteRepository(session)
        self._merchants = MerchantRepository(session)
        self._services = ServiceRepository(session)

    async def lookup_for_transaction(
        self,
        transaction_id: str,
        *,
        account_id: str,
    ) -> EntitlementLookup:
        transaction = await self._transactions.get(transaction_id)
        if transaction is None:
            raise EntitlementNotFoundError(
                "The payment transaction was not found",
                "ENTITLEMENT_TRANSACTION_NOT_FOUND",
            )
        if transaction.account_id != account_id:
            raise EntitlementForbiddenError(
                "The authenticated account does not own this payment transaction",
                "ENTITLEMENT_OWNERSHIP_MISMATCH",
            )
        entitlement = await self._entitlements.get_by_transaction_id(transaction_id)
        if entitlement is not None:
            self.verify_integrity(entitlement)
            return EntitlementLookup(
                transaction_id=transaction_id,
                state="ready",
                reason_code=(
                    "ENTITLEMENT_ACTIVE"
                    if self._as_utc(entitlement.expires_at) > self._read_clock()
                    else "ENTITLEMENT_EXPIRED"
                ),
                entitlement=entitlement,
            )
        state = PaymentTransactionState(transaction.transaction_state)
        if state is not PaymentTransactionState.PAID:
            return EntitlementLookup(
                transaction_id=transaction_id,
                state="blocked",
                reason_code=(
                    "ENTITLEMENT_RECONCILIATION_REQUIRED"
                    if state is PaymentTransactionState.RECONCILIATION_REQUIRED
                    else "ENTITLEMENT_TRANSACTION_NOT_PAID"
                ),
                entitlement=None,
            )
        work = await self._outbox.get_by_deduplication_key(f"entitlement:{transaction_id}")
        if work is None:
            return EntitlementLookup(
                transaction_id=transaction_id,
                state="blocked",
                reason_code="ENTITLEMENT_OUTBOX_MISSING",
                entitlement=None,
            )
        if work.processed_at is not None:
            return EntitlementLookup(
                transaction_id=transaction_id,
                state="blocked",
                reason_code="ENTITLEMENT_ISSUANCE_INTEGRITY_FAILED",
                entitlement=None,
            )
        return EntitlementLookup(
            transaction_id=transaction_id,
            state="pending",
            reason_code=work.last_error_code or "ENTITLEMENT_PENDING",
            entitlement=None,
        )

    async def get_view(
        self,
        entitlement_id: str,
        *,
        account_id: str,
    ) -> EntitlementView:
        entitlement = await self._entitlements.get(entitlement_id)
        if entitlement is None:
            raise EntitlementNotFoundError(
                "The entitlement was not found",
                "ENTITLEMENT_NOT_FOUND",
            )
        if entitlement.account_id != account_id:
            raise EntitlementForbiddenError(
                "The authenticated account does not own this entitlement",
                "ENTITLEMENT_OWNERSHIP_MISMATCH",
            )
        self.verify_integrity(entitlement)
        merchant = await self._merchants.get(entitlement.merchant_id)
        service = await self._services.get(entitlement.service_id)
        if merchant is None or service is None or service.merchant_id != merchant.id:
            raise EntitlementIntegrityError(
                "The entitlement resource binding is unavailable",
                "ENTITLEMENT_INTEGRITY_FAILED",
            )
        return EntitlementView(entitlement=entitlement, merchant=merchant, service=service)

    async def timeline_for_transaction(
        self,
        transaction_id: str,
        *,
        account_id: str,
    ) -> tuple[EntitlementTimelineEntry, ...]:
        transaction = await self._transactions.get(transaction_id)
        if transaction is None:
            raise EntitlementNotFoundError(
                "The payment transaction was not found",
                "ENTITLEMENT_TRANSACTION_NOT_FOUND",
            )
        if transaction.account_id != account_id:
            raise EntitlementForbiddenError(
                "The authenticated account does not own this payment transaction",
                "ENTITLEMENT_OWNERSHIP_MISMATCH",
            )
        payment_events = await self._payment_events.list_for_transaction(transaction_id)
        fulfillment_events = await self._fulfillment_events.list_for_transaction(transaction_id)
        entries = [
            EntitlementTimelineEntry(
                event_type=event.event_type.value.upper(),
                reason_code=event.reason_code,
                occurred_at=self._as_utc(event.occurred_at),
            )
            for event in (*payment_events, *fulfillment_events)
        ]
        work = await self._outbox.get_by_deduplication_key(f"entitlement:{transaction_id}")
        entitlement = await self._entitlements.get_by_transaction_id(transaction_id)
        if work is not None and entitlement is None:
            entries.append(
                EntitlementTimelineEntry(
                    event_type="ENTITLEMENT_PENDING",
                    reason_code=work.last_error_code or "ENTITLEMENT_PENDING",
                    occurred_at=self._as_utc(work.created_at),
                )
            )
        return tuple(sorted(entries, key=lambda entry: entry.occurred_at))

    async def issue_capability(
        self,
        entitlement_id: str,
        *,
        account_id: str,
    ) -> IssuedCapability:
        view = await self.get_view(entitlement_id, account_id=account_id)
        entitlement = view.entitlement
        now = self._read_clock()
        if self._as_utc(entitlement.expires_at) <= now:
            raise EntitlementExpiredError(
                "The entitlement has expired",
                "ENTITLEMENT_EXPIRED",
            )
        await self._payment_service.require_local_value_release_eligibility(
            entitlement.transaction_id,
            for_update=True,
        )
        execution = await self._executions.get_by_entitlement_id(entitlement.id)
        if execution is not None and FulfillmentExecutionState(execution.execution_state) in {
            FulfillmentExecutionState.PERMANENT_FAILURE,
            FulfillmentExecutionState.RECONCILIATION_REQUIRED,
        }:
            raise EntitlementConflictError(
                "The fulfillment cannot accept another capability",
                (
                    "FULFILLMENT_COMPENSATION_REQUIRED"
                    if execution.compensation_required
                    else "FULFILLMENT_RECONCILIATION_REQUIRED"
                ),
            )
        issued = self._capabilities.issue(entitlement)
        self._session.add(
            FulfillmentEvent(
                id=new_fulfillment_event_id(),
                transaction_id=entitlement.transaction_id,
                entitlement_id=entitlement.id,
                execution_id=None,
                execution_revision=None,
                event_type=FulfillmentEventType.CAPABILITY_ISSUED,
                actor_type=FulfillmentEventActorType.ACCOUNT,
                actor_id=account_id,
                reason_code="CAPABILITY_ISSUED",
                event_metadata={
                    "capability_jti": issued.claims.jti,
                    "expires_at": issued.claims.expires_at.isoformat(),
                    "maximum_executions": issued.claims.maximum_executions,
                },
                idempotency_key=f"capability:{issued.claims.jti}",
                occurred_at=now,
            )
        )
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return issued

    async def issue_from_claimed_outbox(
        self,
        event: CommerceOutboxEvent,
        *,
        expected_lease_generation: int,
    ) -> Entitlement:
        """Create one entitlement and finish its claimed work in one final commit."""
        existing = await self._entitlements.get_by_transaction_id(event.aggregate_id)
        if existing is not None:
            self.verify_integrity(existing)
            locked_work = await self._lock_outbox(event.id)
            self._require_current_lease(locked_work, expected_lease_generation)
            locked_work.processed_at = self._read_clock()
            locked_work.last_error_code = None
            await self._session.commit()
            return existing

        proof = await self._payment_service.verify_transaction_for_value_release(event.aggregate_id)
        locked_work = await self._lock_outbox(event.id)
        self._require_current_lease(locked_work, expected_lease_generation)
        transaction = await self._transactions.get_for_update(event.aggregate_id)
        if transaction is None:
            await self._session.rollback()
            raise EntitlementIntegrityError(
                "The payment transaction was not found during entitlement issuance",
                "ENTITLEMENT_PAYMENT_INTEGRITY_FAILED",
            )
        self._require_exact_proof(transaction, proof)

        quote = await self._quotes.get(transaction.quote_id)
        if quote is None:
            await self._session.rollback()
            raise EntitlementIntegrityError(
                "The entitlement quote evidence was not found",
                "ENTITLEMENT_INTEGRITY_FAILED",
            )
        try:
            quote_integrity = verify_quote_integrity(quote)
        except IntegrityStructureError as error:
            await self._session.rollback()
            raise EntitlementIntegrityError(
                "The entitlement quote evidence is malformed",
                "ENTITLEMENT_INTEGRITY_FAILED",
            ) from error
        if not quote_integrity.hash_matches or not self._transaction_matches_quote(
            transaction, quote
        ):
            await self._session.rollback()
            raise EntitlementIntegrityError(
                "The entitlement quote evidence does not match the paid transaction",
                "ENTITLEMENT_INTEGRITY_FAILED",
            )

        now = self._read_clock()
        entitlement_id = new_entitlement_id()
        expires_at = now + self._entitlement_ttl
        hash_fields = {
            "entitlement_id": entitlement_id,
            "account_id": transaction.account_id,
            "transaction_id": transaction.id,
            "payment_binding_hash": transaction.payment_binding_hash,
            "payment_reverification_event_id": proof.payment_reverification_event_id,
            "payment_reverification_revision": proof.transaction_revision,
            "provider_order_id": proof.provider_order_id,
            "provider_payment_id": proof.provider_payment_id,
            "authorization_id": transaction.authorization_id,
            "authorization_hash": transaction.authorization_hash,
            "evaluation_id": transaction.evaluation_id,
            "policy_id": transaction.policy_id,
            "policy_hash": transaction.policy_hash,
            "quote_id": transaction.quote_id,
            "quote_hash": transaction.quote_hash,
            "merchant_id": transaction.merchant_id,
            "service_id": transaction.service_id,
            "input_value": quote.input,
            "input_hash": quote.input_hash,
            "amount": transaction.amount,
            "currency": transaction.currency,
            "purchase_type": transaction.purchase_type,
            "maximum_executions": 1,
            "issued_at": now,
            "expires_at": expires_at,
            "entitlement_version": ENTITLEMENT_HASH_PAYLOAD_VERSION,
        }
        entitlement = Entitlement(
            id=entitlement_id,
            account_id=transaction.account_id,
            transaction_id=transaction.id,
            payment_binding_hash=transaction.payment_binding_hash,
            payment_reverification_event_id=proof.payment_reverification_event_id,
            payment_reverification_revision=proof.transaction_revision,
            provider_order_id=proof.provider_order_id,
            provider_payment_id=proof.provider_payment_id,
            authorization_id=transaction.authorization_id,
            authorization_hash=transaction.authorization_hash,
            evaluation_id=transaction.evaluation_id,
            policy_id=transaction.policy_id,
            policy_hash=transaction.policy_hash,
            quote_id=transaction.quote_id,
            quote_hash=transaction.quote_hash,
            merchant_id=transaction.merchant_id,
            service_id=transaction.service_id,
            input=quote.input,
            input_hash=quote.input_hash,
            amount=transaction.amount,
            currency=transaction.currency,
            purchase_type=transaction.purchase_type,
            maximum_executions=1,
            issued_at=now,
            expires_at=expires_at,
            entitlement_version=ENTITLEMENT_HASH_PAYLOAD_VERSION,
            entitlement_hash=calculate_entitlement_hash(**hash_fields),
            created_at=now,
        )
        payment_event = FulfillmentEvent(
            id=new_fulfillment_event_id(),
            transaction_id=transaction.id,
            entitlement_id=None,
            execution_id=None,
            execution_revision=None,
            event_type=FulfillmentEventType.PAYMENT_REVERIFIED,
            actor_type=FulfillmentEventActorType.ENTITLEMENT_WORKER,
            actor_id=locked_work.id,
            reason_code="PAYMENT_REVERIFIED_FOR_VALUE_RELEASE",
            event_metadata={
                "payment_transaction_event_id": proof.payment_reverification_event_id,
                "transaction_revision": proof.transaction_revision,
                "provider_order_id": proof.provider_order_id,
                "provider_payment_id": proof.provider_payment_id,
            },
            idempotency_key=f"entitlement-payment-proof:{transaction.id}",
            occurred_at=proof.verified_at,
        )
        issued_event = FulfillmentEvent(
            id=new_fulfillment_event_id(),
            transaction_id=transaction.id,
            entitlement_id=entitlement.id,
            execution_id=None,
            execution_revision=None,
            event_type=FulfillmentEventType.ENTITLEMENT_ISSUED,
            actor_type=FulfillmentEventActorType.ENTITLEMENT_WORKER,
            actor_id=locked_work.id,
            reason_code="ENTITLEMENT_ISSUED",
            event_metadata={
                "entitlement_hash": entitlement.entitlement_hash,
                "expires_at": expires_at.isoformat(),
                "maximum_executions": 1,
            },
            idempotency_key=f"entitlement-issued:{transaction.id}",
            occurred_at=now,
        )
        locked_work.processed_at = now
        locked_work.last_error_code = None
        self._session.add(payment_event)
        try:
            return await self._entitlements.create_with_event(
                entitlement,
                event=issued_event,
            )
        except IntegrityError:
            await self._session.rollback()
            existing = await self._entitlements.get_by_transaction_id(transaction.id)
            if existing is None:
                raise
            self.verify_integrity(existing)
            work = await self._lock_outbox(event.id)
            self._require_current_lease(work, expected_lease_generation)
            work.processed_at = self._read_clock()
            work.last_error_code = None
            await self._session.commit()
            return existing

    @staticmethod
    def verify_integrity(entitlement: Entitlement) -> None:
        try:
            integrity = recompute_entitlement_integrity(entitlement)
        except (TypeError, ValueError) as error:
            raise EntitlementIntegrityError(
                "The entitlement evidence is malformed",
                "ENTITLEMENT_INTEGRITY_FAILED",
            ) from error
        if not compare_digest(integrity.input_hash, entitlement.input_hash) or not (
            compare_digest(integrity.entitlement_hash, entitlement.entitlement_hash)
        ):
            raise EntitlementIntegrityError(
                "The entitlement integrity hash does not match",
                "ENTITLEMENT_INTEGRITY_FAILED",
            )

    async def _lock_outbox(self, event_id: str) -> CommerceOutboxEvent:
        event = await self._session.scalar(
            select(CommerceOutboxEvent)
            .where(CommerceOutboxEvent.id == event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if event is None:
            raise EntitlementIntegrityError(
                "The entitlement outbox work was not found",
                "ENTITLEMENT_OUTBOX_MISSING",
            )
        return event

    @staticmethod
    def _require_current_lease(
        event: CommerceOutboxEvent,
        expected_lease_generation: int,
    ) -> None:
        if (
            event.processed_at is not None
            or event.lease_generation != expected_lease_generation
            or event.processing_started_at is None
            or event.lease_expires_at is None
        ):
            raise EntitlementConflictError(
                "The entitlement outbox lease is no longer current",
                "ENTITLEMENT_WORK_LEASE_LOST",
            )

    @staticmethod
    def _require_exact_proof(transaction: object, proof: PaymentValueReleaseProof) -> None:
        if (
            transaction.id != proof.transaction_id
            or transaction.revision != proof.transaction_revision
            or PaymentTransactionState(transaction.transaction_state)
            is not PaymentTransactionState.PAID
            or transaction.provider_order_id != proof.provider_order_id
        ):
            raise EntitlementConflictError(
                "Payment evidence changed after provider re-verification",
                "ENTITLEMENT_RECONCILIATION_REQUIRED",
            )

    @staticmethod
    def _transaction_matches_quote(transaction: object, quote: object) -> bool:
        return (
            transaction.quote_id == quote.id
            and compare_digest(transaction.quote_hash, quote.quote_hash)
            and transaction.merchant_id == quote.merchant_id
            and transaction.service_id == quote.service_id
            and transaction.amount == quote.amount
            and transaction.currency == quote.currency
            and transaction.purchase_type == quote.purchase_type
        )

    def _read_clock(self) -> datetime:
        return self._as_utc(self._clock())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Entitlement timestamps must be timezone-aware")
        return value.astimezone(UTC)
