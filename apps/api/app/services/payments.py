"""Authorization-backed Razorpay transaction orchestration and reconciliation."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Any, Protocol

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.payment_webhooks import RedisPaymentWebhookQueue, VerifiedWebhookPayload
from app.core.config import Settings, get_settings
from app.db.session import Database, create_database
from app.domain.approval_hashing import recompute_authorization_hash
from app.domain.enums import (
    ApprovalIdentityStatus,
    MerchantStatus,
    PaymentAttemptStatus,
    PaymentEventActorType,
    PaymentTransactionEventType,
    PaymentTransactionState,
    PolicyDecision,
    PurchaseType,
    RazorpayOrderStatus,
    ServiceStatus,
    WebhookProcessingStatus,
)
from app.domain.enums import (
    PaymentProvider as PaymentProviderName,
)
from app.domain.exceptions import (
    AuthenticationForbiddenError,
    EntitlementConflictError,
    EntitlementIntegrityError,
    EntitlementUnavailableError,
    PaymentConflictError,
    PaymentExpiredError,
    PaymentIntegrityError,
    PaymentNotFoundError,
    PaymentTimeoutError,
    PaymentUnavailableError,
    PaymentVerificationError,
)
from app.domain.hashing import canonical_utc_datetime, sha256_bytes, sha256_json
from app.domain.ids import (
    new_payment_attempt_id,
    new_payment_transaction_event_id,
    new_payment_transaction_id,
    new_razorpay_webhook_event_id,
)
from app.domain.integrity import IntegrityStructureError
from app.domain.payment_hashing import (
    PAYMENT_BINDING_VERSION,
    calculate_payment_binding_hash,
    recompute_payment_binding_hash,
)
from app.domain.payment_state import (
    can_transition_attempt_status,
    can_transition_order_status,
    can_transition_transaction,
)
from app.domain.policy_hashing import verify_policy_integrity
from app.domain.quote_integrity import verify_quote_integrity
from app.models import (
    PaymentAttempt,
    PaymentTransaction,
    PaymentTransactionEvent,
    RazorpayWebhookEvent,
)
from app.providers import (
    CreateProviderOrder,
    PaymentProvider,
    PaymentProviderRejectedError,
    PaymentProviderResponseError,
    PaymentProviderTimeoutError,
    PaymentProviderUnavailableError,
    ProviderOrder,
    ProviderPayment,
    RazorpayPaymentProvider,
    validate_provider_event_type,
    verify_checkout_signature,
)
from app.providers.base import validate_provider_order_id, validate_provider_payment_id
from app.repositories import (
    ApprovalIdentityRepository,
    BuyerPolicyRepository,
    MerchantRepository,
    PaymentAttemptRepository,
    PaymentTransactionEventRepository,
    PaymentTransactionRepository,
    PolicyEvaluationRepository,
    PurchaseAuthorizationRepository,
    QuoteRepository,
    RazorpayWebhookEventRepository,
    ServiceRepository,
)
from app.schemas.approvals import AuthorizationParty
from app.schemas.payments import (
    CheckoutVerificationCreate,
    PaymentAttemptResponse,
    PaymentTransactionCreate,
    PaymentTransactionResponse,
    RazorpayCheckoutConfiguration,
)
from app.services.policy_evaluations import PolicyEvaluationApplicationService
from app.workers.razorpay_webhooks import RazorpayWebhookWorker

Clock = Callable[[], datetime]
_VALUE_REVOKING_WEBHOOK_EVENTS = frozenset(
    {"refund.created", "refund.processed", "refund.speed_changed"}
)
_SUPPORTED_WEBHOOK_EVENTS = (
    frozenset(
        {
            "payment.authorized",
            "payment.captured",
            "payment.failed",
            "order.paid",
            "refund.failed",
        }
    )
    | _VALUE_REVOKING_WEBHOOK_EVENTS
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PaymentOperationResult:
    """A transaction representation plus its intended successful HTTP status."""

    response: PaymentTransactionResponse
    status_code: int


@dataclass(frozen=True, slots=True)
class PaymentValueReleaseProof:
    """Fresh provider evidence fenced to one payment aggregate revision."""

    transaction_id: str
    transaction_revision: int
    payment_reverification_event_id: str
    provider_order_id: str
    provider_payment_id: str
    verified_at: datetime


class PaymentValueReleaseEligibility(Protocol):
    """Payment gates shared by every post-payment value release."""

    async def require_local_value_release_eligibility(
        self,
        transaction_id: str,
        *,
        for_update: bool = False,
    ) -> PaymentTransaction: ...

    async def verify_transaction_for_value_release(
        self,
        transaction_id: str,
    ) -> PaymentValueReleaseProof: ...


@dataclass(frozen=True, slots=True)
class _WebhookEnvelope:
    event_type: str
    provider_created_at: datetime
    provider_order_id: str | None
    provider_payment_id: str | None


class PaymentApplicationService:
    """Keep browser and provider evidence behind one conservative money boundary."""

    def __init__(
        self,
        session: AsyncSession,
        provider: PaymentProvider | None,
        settings: Settings,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._session = session
        self._provider = provider
        self._settings = settings
        self._clock = clock
        self._authorizations = PurchaseAuthorizationRepository(session)
        self._identities = ApprovalIdentityRepository(session)
        self._policies = BuyerPolicyRepository(session)
        self._quotes = QuoteRepository(session)
        self._evaluations = PolicyEvaluationRepository(session)
        self._merchants = MerchantRepository(session)
        self._services = ServiceRepository(session)
        self._transactions = PaymentTransactionRepository(session)
        self._attempts = PaymentAttemptRepository(session)
        self._events = PaymentTransactionEventRepository(session)
        self._webhooks = RazorpayWebhookEventRepository(session)
        self._evaluation_service = PolicyEvaluationApplicationService(
            self._policies,
            self._quotes,
            self._evaluations,
            clock=clock,
        )

    async def create_transaction(
        self,
        payload: PaymentTransactionCreate,
        *,
        account_id: str,
    ) -> PaymentOperationResult:
        """Atomically claim an authorization, then create its Order outside PostgreSQL."""
        self._require_enabled()
        authorization = await self._authorizations.get(payload.authorization_id)
        if authorization is None:
            raise PaymentNotFoundError(
                f"Purchase authorization '{payload.authorization_id}' was not found",
                "PAYMENT_AUTHORIZATION_NOT_FOUND",
            )
        identity = await self._identities.get_for_update(authorization.approval_identity_id)
        if identity is None or identity.account_id != account_id:
            raise AuthenticationForbiddenError(
                "The authenticated account does not own this purchase authorization",
                "AUTH_RESOURCE_OWNERSHIP_MISMATCH",
            )
        if ApprovalIdentityStatus(identity.status) is not ApprovalIdentityStatus.ACTIVE:
            raise PaymentConflictError(
                "The approval identity is disabled",
                "PAYMENT_APPROVAL_IDENTITY_DISABLED",
            )
        authorization = await self._authorizations.get_for_update(payload.authorization_id)
        if authorization is None or authorization.approval_identity_id != identity.id:
            raise PaymentIntegrityError(
                "Purchase authorization ownership changed during payment claim",
                "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED",
            )

        existing = await self._transactions.get_by_authorization_id_for_update(authorization.id)
        if existing is not None:
            self._require_transaction_owner(existing, account_id)
            self._verify_transaction_integrity(existing)
            await self._session.commit()
            if self._should_reconcile_order_creation(existing):
                existing = await self._reconcile_order_creation(existing.id)
            return PaymentOperationResult(
                response=await self._response(existing),
                status_code=self._pending_status(existing),
            )

        now = self._read_clock()
        await self._validate_new_claim(
            authorization, identity_subject=identity.subject_ref, now=now
        )
        transaction_id = new_payment_transaction_id()
        binding_fields: dict[str, Any] = {
            "transaction_id": transaction_id,
            "payment_binding_version": PAYMENT_BINDING_VERSION,
            "account_id": account_id,
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
            "provider": PaymentProviderName.RAZORPAY,
            "provider_receipt": transaction_id,
        }
        transaction = PaymentTransaction(
            id=transaction_id,
            account_id=account_id,
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
            provider=PaymentProviderName.RAZORPAY,
            provider_receipt=transaction_id,
            provider_order_id=None,
            provider_order_status=None,
            transaction_state=PaymentTransactionState.ORDER_CREATION_PENDING,
            order_creation_attempts=1,
            order_creation_started_at=now,
            order_created_at=None,
            paid_at=None,
            last_reconciled_at=None,
            payment_binding_version=PAYMENT_BINDING_VERSION,
            payment_binding_hash=calculate_payment_binding_hash(**binding_fields),
            revision=1,
        )
        initial_event = self._event(
            transaction,
            event_type=PaymentTransactionEventType.PAYMENT_TRANSACTION_CREATED,
            actor_type=PaymentEventActorType.ACCOUNT,
            actor_id=account_id,
            prior_state=None,
            resulting_state=PaymentTransactionState.ORDER_CREATION_PENDING,
            reason_code="PAYMENT_TRANSACTION_CREATED",
            metadata={
                "authorization_id": authorization.id,
                "provider": PaymentProviderName.RAZORPAY.value,
                "receipt": transaction_id,
            },
            idempotency_key=f"{transaction_id}:claim",
            occurred_at=now,
            revision=1,
        )
        try:
            transaction = await self._transactions.create_with_event(
                transaction,
                event=initial_event,
            )
        except IntegrityError:
            await self._session.rollback()
            existing = await self._transactions.get_by_authorization_id(authorization.id)
            if existing is None:
                raise
            self._require_transaction_owner(existing, account_id)
            self._verify_transaction_integrity(existing)
            return PaymentOperationResult(
                response=await self._response(existing),
                status_code=self._pending_status(existing),
            )

        transaction = await self._record_simple_transition(
            transaction.id,
            next_state=PaymentTransactionState.ORDER_CREATION_PENDING,
            event_type=PaymentTransactionEventType.RAZORPAY_ORDER_CREATION_STARTED,
            reason_code="PAYMENT_ORDER_CREATION_STARTED",
            actor_type=PaymentEventActorType.SYSTEM,
        )
        transaction = await self._create_provider_order(transaction.id)
        return PaymentOperationResult(
            response=await self._response(transaction),
            status_code=201 if transaction.provider_order_id is not None else 202,
        )

    async def get_transaction(
        self,
        transaction_id: str,
        *,
        account_id: str,
    ) -> PaymentTransactionResponse:
        transaction = await self._get_owned_transaction(transaction_id, account_id=account_id)
        return await self._response(transaction)

    async def require_local_value_release_eligibility(
        self,
        transaction_id: str,
        *,
        for_update: bool = False,
    ) -> PaymentTransaction:
        """Require locally durable paid evidence without contacting the provider."""
        transaction = (
            await self._transactions.get_for_update(transaction_id)
            if for_update
            else await self._transactions.get(transaction_id)
        )
        if transaction is None:
            raise EntitlementConflictError(
                "The payment transaction was not found",
                "ENTITLEMENT_TRANSACTION_NOT_PAID",
            )
        try:
            self._verify_transaction_integrity(transaction)
        except PaymentIntegrityError as error:
            raise EntitlementIntegrityError(
                "The payment transaction failed integrity verification",
                "ENTITLEMENT_PAYMENT_INTEGRITY_FAILED",
            ) from error
        state = PaymentTransactionState(transaction.transaction_state)
        if state is not PaymentTransactionState.PAID:
            raise EntitlementConflictError(
                "The payment transaction is not paid",
                (
                    "ENTITLEMENT_RECONCILIATION_REQUIRED"
                    if state is PaymentTransactionState.RECONCILIATION_REQUIRED
                    else "ENTITLEMENT_TRANSACTION_NOT_PAID"
                ),
            )
        if await self._unresolved_reconciliation_revision(transaction.id) is not None:
            raise EntitlementConflictError(
                "Payment reconciliation evidence must be explicitly resolved before value release",
                "ENTITLEMENT_RECONCILIATION_REQUIRED",
            )
        return transaction

    async def verify_transaction_for_value_release(
        self,
        transaction_id: str,
    ) -> PaymentValueReleaseProof:
        """Reverify an already-paid transaction and return a revision-fenced proof.

        Provider I/O deliberately happens without a PostgreSQL row lock. The final
        PAYMENT_REVERIFIED event is appended under the aggregate lock; entitlement
        issuance must then require that exact revision before committing value.
        """
        try:
            self._require_enabled()
        except PaymentUnavailableError as error:
            raise EntitlementUnavailableError(
                "Payment re-verification is disabled or unavailable",
                "ENTITLEMENT_PAYMENT_REVERIFICATION_FAILED",
            ) from error
        transaction = await self.require_local_value_release_eligibility(transaction_id)
        if transaction.provider_order_id is None:
            raise EntitlementIntegrityError(
                "The paid transaction has no provider order binding",
                "ENTITLEMENT_PAYMENT_INTEGRITY_FAILED",
            )
        provider_order_id = transaction.provider_order_id
        await self._session.commit()

        try:
            provider = self._require_provider()
        except PaymentUnavailableError as error:
            raise EntitlementUnavailableError(
                "Payment provider re-verification is temporarily unavailable",
                "ENTITLEMENT_PAYMENT_REVERIFICATION_FAILED",
            ) from error
        try:
            order, payments = await asyncio.gather(
                provider.fetch_order(provider_order_id),
                provider.fetch_payments_for_order(provider_order_id),
            )
        except (PaymentProviderTimeoutError, PaymentProviderUnavailableError) as error:
            raise EntitlementUnavailableError(
                "Payment provider re-verification is temporarily unavailable",
                "ENTITLEMENT_PAYMENT_REVERIFICATION_FAILED",
            ) from error
        except (PaymentProviderRejectedError, PaymentProviderResponseError) as error:
            await self._mark_reconciliation_required(
                transaction_id,
                reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=provider_order_id,
                metadata={"value_release_reverification": True},
            )
            raise EntitlementConflictError(
                "Payment provider re-verification returned inconsistent evidence",
                "ENTITLEMENT_RECONCILIATION_REQUIRED",
            ) from error

        captured_payment = self._validate_value_release_evidence(
            transaction,
            order=order,
            payments=payments,
        )
        if captured_payment is None:
            await self._mark_reconciliation_required(
                transaction_id,
                reason_code="PAYMENT_VALUE_RELEASE_EVIDENCE_INVALID",
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=provider_order_id,
                metadata={"value_release_reverification": True},
            )
            raise EntitlementConflictError(
                "Payment provider evidence cannot release merchant value",
                "ENTITLEMENT_RECONCILIATION_REQUIRED",
            )

        # Normalize the same full snapshot into the existing payment-attempt audit
        # stream before recording the stronger value-release observation.
        snapshot_key = (
            f"value-release-snapshot:{transaction_id}:{transaction.revision + 1}:"
            f"{captured_payment.id}:{captured_payment.status}:"
            f"{captured_payment.amount_refunded}"
        )
        normalized = await self._apply_provider_evidence(
            transaction_id,
            order=order,
            payments=payments,
            actor_type=PaymentEventActorType.PROVIDER_API,
            actor_id=captured_payment.id,
            idempotency_key=snapshot_key,
            authoritative_snapshot=True,
        )
        normalized_event = await self._events.get_by_idempotency_key(snapshot_key)
        if (
            normalized_event is None
            or normalized_event.transaction_revision != normalized.revision
            or PaymentTransactionEventType(normalized_event.event_type)
            is PaymentTransactionEventType.RECONCILIATION_REQUIRED
            or normalized_event.event_metadata.get("authoritative_snapshot") is not True
        ):
            raise EntitlementConflictError(
                "Payment evidence normalization detected a reconciliation anomaly",
                "ENTITLEMENT_RECONCILIATION_REQUIRED",
            )

        try:
            locked = await self.require_local_value_release_eligibility(
                transaction_id,
                for_update=True,
            )
        except (EntitlementConflictError, EntitlementIntegrityError):
            await self._session.rollback()
            raise
        if (
            locked.provider_order_id != order.id
            or locked.revision != normalized_event.transaction_revision
        ):
            await self._session.rollback()
            raise EntitlementConflictError(
                "Payment state changed during re-verification",
                "ENTITLEMENT_RECONCILIATION_REQUIRED",
            )
        local_attempts = await self._attempts.list_for_transaction(locked.id)
        local_captures = [
            attempt
            for attempt in local_attempts
            if attempt.provider_payment_id == captured_payment.id
            and attempt.captured
            and PaymentAttemptStatus(attempt.provider_status) is PaymentAttemptStatus.CAPTURED
            and attempt.provider_order_id == captured_payment.order_id
            and attempt.amount == captured_payment.amount
            and attempt.currency == captured_payment.currency
            and self._as_utc(attempt.provider_created_at) == captured_payment.created_at
        ]
        if len(local_captures) != 1:
            await self._session.rollback()
            raise EntitlementIntegrityError(
                "Captured payment evidence was not durably normalized",
                "ENTITLEMENT_PAYMENT_INTEGRITY_FAILED",
            )

        now = self._reconciliation_time(locked)
        prior = PaymentTransactionState(locked.transaction_state)
        event = self._event(
            locked,
            event_type=PaymentTransactionEventType.PAYMENT_REVERIFIED,
            actor_type=PaymentEventActorType.PROVIDER_API,
            actor_id=captured_payment.id,
            prior_state=prior,
            resulting_state=prior,
            reason_code="PAYMENT_REVERIFIED_FOR_VALUE_RELEASE",
            metadata={
                "authoritative_snapshot": True,
                "value_release_reverification": True,
                "provider_order_id": order.id,
                "provider_order_status": order.status,
                "provider_payment_id": captured_payment.id,
                "provider_payment_status": captured_payment.status,
                "amount_refunded": captured_payment.amount_refunded,
            },
            payment_attempt_id=local_captures[0].id,
            idempotency_key=(
                f"value-release-proof:{locked.id}:{locked.revision + 1}:{captured_payment.id}"
            ),
            occurred_at=now,
        )
        locked.last_reconciled_at = now
        locked = await self._transactions.update_with_event(locked, event=event)
        return PaymentValueReleaseProof(
            transaction_id=locked.id,
            transaction_revision=locked.revision,
            payment_reverification_event_id=event.id,
            provider_order_id=order.id,
            provider_payment_id=captured_payment.id,
            verified_at=now,
        )

    async def verify_checkout(
        self,
        transaction_id: str,
        payload: CheckoutVerificationCreate,
        *,
        account_id: str,
    ) -> PaymentOperationResult:
        """Verify browser binding, then obtain capture truth from Razorpay APIs."""
        self._require_enabled()
        provider = self._require_provider()
        transaction = await self._transactions.get_for_update(transaction_id)
        if transaction is None:
            raise PaymentNotFoundError(
                f"Payment transaction '{transaction_id}' was not found",
                "PAYMENT_TRANSACTION_NOT_FOUND",
            )
        self._require_transaction_owner(transaction, account_id)
        self._verify_transaction_integrity(transaction)
        if transaction.provider_order_id is None:
            raise PaymentConflictError(
                "The payment transaction has no verified provider order",
                "PAYMENT_ORDER_NOT_READY",
            )
        if payload.razorpay_order_id != transaction.provider_order_id:
            raise PaymentVerificationError(
                "Checkout returned a different Razorpay Order",
                "PAYMENT_CHECKOUT_ORDER_MISMATCH",
            )
        key_secret = self._key_secret()
        if not verify_checkout_signature(
            order_id=transaction.provider_order_id,
            payment_id=payload.razorpay_payment_id,
            signature=payload.razorpay_signature.lower(),
            key_secret=key_secret,
        ):
            raise PaymentVerificationError(
                "Razorpay Checkout signature is invalid",
                "PAYMENT_CHECKOUT_SIGNATURE_INVALID",
            )

        signature_hash = sha256_bytes(payload.razorpay_signature.lower().encode("ascii"))
        signature_key = f"checkout:{transaction.id}:{payload.razorpay_payment_id}:{signature_hash}"
        prior_signature_event = await self._events.get_by_idempotency_key(signature_key)
        if prior_signature_event is None:
            now = self._read_clock()
            state = PaymentTransactionState(transaction.transaction_state)
            event = self._event(
                transaction,
                event_type=PaymentTransactionEventType.CHECKOUT_SIGNATURE_VERIFIED,
                actor_type=PaymentEventActorType.ACCOUNT,
                actor_id=account_id,
                prior_state=state,
                resulting_state=state,
                reason_code="PAYMENT_CHECKOUT_SIGNATURE_VERIFIED",
                metadata={
                    "provider_payment_id": payload.razorpay_payment_id,
                    "signature_hash": signature_hash,
                },
                idempotency_key=signature_key,
                occurred_at=now,
            )
            transaction = await self._transactions.update_with_event(transaction, event=event)
        else:
            await self._session.commit()

        try:
            payment, order = await asyncio.gather(
                provider.fetch_payment(payload.razorpay_payment_id),
                provider.fetch_order(payload.razorpay_order_id),
            )
        except (
            PaymentProviderTimeoutError,
            PaymentProviderUnavailableError,
        ):
            transaction = await self._mark_reconciliation_required(
                transaction.id,
                reason_code="PAYMENT_PROVIDER_UNAVAILABLE",
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=payload.razorpay_payment_id,
            )
            return PaymentOperationResult(await self._response(transaction), 202)
        except (PaymentProviderRejectedError, PaymentProviderResponseError) as error:
            transaction = await self._mark_reconciliation_required(
                transaction.id,
                reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=payload.razorpay_payment_id,
                metadata={"failure_type": type(error).__name__},
            )
            return PaymentOperationResult(await self._response(transaction), 202)

        if payment.id != payload.razorpay_payment_id:
            transaction = await self._mark_reconciliation_required(
                transaction.id,
                reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=payload.razorpay_payment_id,
                metadata={
                    "expected_provider_payment_id": payload.razorpay_payment_id,
                    "actual_provider_payment_id": payment.id,
                },
            )
            return PaymentOperationResult(await self._response(transaction), 202)

        transaction = await self._apply_provider_evidence(
            transaction.id,
            order=order,
            payments=(payment,),
            actor_type=PaymentEventActorType.PROVIDER_API,
            actor_id=payment.id,
            idempotency_key=(
                f"callback:{transaction.id}:{payment.id}:{payment.status}:"
                f"{int(payment.captured)}:{payment.amount_refunded}:{order.status}"
            ),
            authoritative_snapshot=False,
        )
        return PaymentOperationResult(
            response=await self._response(transaction),
            status_code=self._pending_status(transaction),
        )

    async def reconcile_transaction(
        self,
        transaction_id: str,
        *,
        account_id: str,
    ) -> PaymentOperationResult:
        """Rebuild local aggregate state from exact server-fetched provider evidence."""
        self._require_enabled()
        transaction = await self._transactions.get_for_update(transaction_id)
        if transaction is None:
            raise PaymentNotFoundError(
                f"Payment transaction '{transaction_id}' was not found",
                "PAYMENT_TRANSACTION_NOT_FOUND",
            )
        self._require_transaction_owner(transaction, account_id)
        self._verify_transaction_integrity(transaction)
        await self._session.commit()
        if transaction.provider_order_id is None:
            transaction = await self._reconcile_order_creation(transaction.id)
            if transaction.provider_order_id is None:
                return PaymentOperationResult(await self._response(transaction), 202)
        transaction = await self._fetch_and_apply_order(
            transaction.id,
            actor_type=PaymentEventActorType.ACCOUNT,
            actor_id=account_id,
            idempotency_key=f"reconcile:{transaction.id}:{self._read_clock().isoformat()}",
        )
        return PaymentOperationResult(
            await self._response(transaction),
            self._pending_status(transaction),
        )

    async def process_webhook(self, payload: VerifiedWebhookPayload) -> None:
        """Process one signed queue delivery with DB uniqueness as the effect fence."""
        self._require_enabled()
        await self.quarantine_value_revoking_webhook(payload)
        body_hash = sha256_bytes(payload.raw_body)
        existing = await self._webhooks.get_by_provider_event_id(payload.provider_event_id)
        if existing is not None:
            if not compare_digest(existing.raw_body_hash, body_hash):
                raise PaymentIntegrityError(
                    "A Razorpay event ID was reused with different signed content",
                    "PAYMENT_WEBHOOK_EVENT_INTEGRITY_FAILED",
                ) from None
            return

        try:
            envelope = self._parse_webhook_envelope(payload.raw_body)
        except (TypeError, ValueError):
            await self._persist_unmatched_webhook(
                payload,
                body_hash=body_hash,
                event_type="invalid",
                provider_created_at=payload.received_at,
                reason_code="PAYMENT_WEBHOOK_PAYLOAD_INVALID",
                status=WebhookProcessingStatus.FAILED,
            )
            return

        if envelope.event_type not in _SUPPORTED_WEBHOOK_EVENTS:
            await self._persist_unmatched_webhook(
                payload,
                body_hash=body_hash,
                event_type=envelope.event_type,
                provider_created_at=envelope.provider_created_at,
                reason_code="PAYMENT_WEBHOOK_EVENT_IGNORED",
                status=WebhookProcessingStatus.IGNORED,
                provider_order_id=envelope.provider_order_id,
                provider_payment_id=envelope.provider_payment_id,
            )
            return
        if envelope.provider_order_id is None:
            await self._persist_unmatched_webhook(
                payload,
                body_hash=body_hash,
                event_type=envelope.event_type,
                provider_created_at=envelope.provider_created_at,
                reason_code="PAYMENT_WEBHOOK_ORDER_MISSING",
                status=WebhookProcessingStatus.FAILED,
                provider_payment_id=envelope.provider_payment_id,
            )
            return

        provider = self._require_provider()
        transaction = await self._transactions.get_by_provider_order_id(envelope.provider_order_id)
        recovered_order: ProviderOrder | None = None
        if transaction is None:
            await self._session.commit()
            try:
                recovered_order = await provider.fetch_order(envelope.provider_order_id)
            except (PaymentProviderRejectedError, PaymentProviderResponseError):
                await self._persist_unmatched_webhook(
                    payload,
                    body_hash=body_hash,
                    event_type=envelope.event_type,
                    provider_created_at=envelope.provider_created_at,
                    reason_code="PAYMENT_WEBHOOK_ORDER_UNMATCHED",
                    status=WebhookProcessingStatus.IGNORED,
                    provider_order_id=envelope.provider_order_id,
                    provider_payment_id=envelope.provider_payment_id,
                )
                return
            transaction = await self._transactions.get_by_provider_receipt(recovered_order.receipt)
            if transaction is None:
                await self._persist_unmatched_webhook(
                    payload,
                    body_hash=body_hash,
                    event_type=envelope.event_type,
                    provider_created_at=envelope.provider_created_at,
                    reason_code="PAYMENT_WEBHOOK_ORDER_UNMATCHED",
                    status=WebhookProcessingStatus.IGNORED,
                    provider_order_id=envelope.provider_order_id,
                    provider_payment_id=envelope.provider_payment_id,
                )
                return
            transaction = await self._attach_provider_order(
                transaction.id,
                recovered_order,
            )

        if envelope.event_type in _VALUE_REVOKING_WEBHOOK_EVENTS:
            # The first ingress quarantine may have raced a local Order
            # attachment. Once receipt recovery or a concurrent attachment has
            # resolved this provider Order to a transaction, fence the signed
            # refund evidence under that aggregate lock before any commit or
            # additional provider read can expose a paid release window.
            locked = await self._transactions.get_for_update(transaction.id)
            if locked is None:
                raise PaymentNotFoundError(
                    f"Payment transaction '{transaction.id}' was not found",
                    "PAYMENT_TRANSACTION_NOT_FOUND",
                ) from None
            await self._quarantine_locked_value_revoking_webhook(
                payload,
                envelope=envelope,
                body_hash=body_hash,
                transaction=locked,
            )
            return

        await self._session.commit()
        try:
            if recovered_order is None:
                order, payments = await asyncio.gather(
                    provider.fetch_order(envelope.provider_order_id),
                    provider.fetch_payments_for_order(envelope.provider_order_id),
                )
            else:
                order = recovered_order
                payments = await provider.fetch_payments_for_order(envelope.provider_order_id)
        except (PaymentProviderRejectedError, PaymentProviderResponseError):
            locked = await self._transactions.get_for_update(transaction.id)
            if locked is None:
                raise PaymentNotFoundError(
                    f"Payment transaction '{transaction.id}' was not found",
                    "PAYMENT_TRANSACTION_NOT_FOUND",
                ) from None
            webhook = RazorpayWebhookEvent(
                id=new_razorpay_webhook_event_id(),
                provider_event_id=payload.provider_event_id,
                provider_event_type=envelope.event_type,
                raw_body_hash=body_hash,
                provider_created_at=envelope.provider_created_at,
                received_at=payload.received_at,
                processed_at=self._processed_at(payload.received_at),
                processing_status=WebhookProcessingStatus.RECONCILIATION_REQUIRED,
                processing_reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                provider_order_id=envelope.provider_order_id,
                provider_payment_id=envelope.provider_payment_id,
                transaction_id=locked.id,
            )
            await self._mark_locked_reconciliation_required(
                locked,
                reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                actor_type=PaymentEventActorType.PROVIDER_WEBHOOK,
                actor_id=payload.provider_event_id,
                webhook_event=webhook,
                idempotency_key=f"webhook:{payload.provider_event_id}",
            )
            return

        webhook_id = new_razorpay_webhook_event_id()
        webhook = RazorpayWebhookEvent(
            id=webhook_id,
            provider_event_id=payload.provider_event_id,
            provider_event_type=envelope.event_type,
            raw_body_hash=body_hash,
            provider_created_at=envelope.provider_created_at,
            received_at=payload.received_at,
            processed_at=self._processed_at(payload.received_at),
            processing_status=WebhookProcessingStatus.PROCESSED,
            processing_reason_code="PAYMENT_WEBHOOK_PROCESSED",
            provider_order_id=envelope.provider_order_id,
            provider_payment_id=envelope.provider_payment_id,
            transaction_id=transaction.id,
        )
        await self._apply_provider_evidence(
            transaction.id,
            order=order,
            payments=payments,
            actor_type=PaymentEventActorType.PROVIDER_WEBHOOK,
            actor_id=payload.provider_event_id,
            idempotency_key=f"webhook:{payload.provider_event_id}",
            webhook_event=webhook,
            authoritative_snapshot=True,
        )

    async def quarantine_value_revoking_webhook(
        self,
        payload: VerifiedWebhookPayload,
    ) -> None:
        """Persist signed refund evidence before acknowledging its queue admission."""
        self._require_enabled()
        try:
            envelope = self._parse_webhook_envelope(payload.raw_body)
        except (TypeError, ValueError):
            return
        if envelope.event_type not in _VALUE_REVOKING_WEBHOOK_EVENTS:
            return
        body_hash = sha256_bytes(payload.raw_body)
        existing = await self._webhooks.get_by_provider_event_id(payload.provider_event_id)
        if existing is not None:
            if not compare_digest(existing.raw_body_hash, body_hash):
                raise PaymentIntegrityError(
                    "A Razorpay event ID was reused with different signed content",
                    "PAYMENT_WEBHOOK_EVENT_INTEGRITY_FAILED",
                )
            await self._session.rollback()
            return
        if envelope.provider_order_id is None:
            await self._persist_unmatched_webhook(
                payload,
                body_hash=body_hash,
                event_type=envelope.event_type,
                provider_created_at=envelope.provider_created_at,
                reason_code="PAYMENT_WEBHOOK_ORDER_MISSING",
                status=WebhookProcessingStatus.RECONCILIATION_REQUIRED,
                provider_payment_id=envelope.provider_payment_id,
            )
            return
        transaction = await self._transactions.get_by_provider_order_id_for_update(
            envelope.provider_order_id
        )
        if transaction is None:
            # The Order may exist at Razorpay before its ID is durably attached
            # to the local transaction. Ingress cannot call the provider to map
            # the Order back to its receipt, so leave this event unconsumed for
            # the queued worker's provider-backed receipt recovery path.
            return
        await self._quarantine_locked_value_revoking_webhook(
            payload,
            envelope=envelope,
            body_hash=body_hash,
            transaction=transaction,
        )

    async def _quarantine_locked_value_revoking_webhook(
        self,
        payload: VerifiedWebhookPayload,
        *,
        envelope: _WebhookEnvelope,
        body_hash: str,
        transaction: PaymentTransaction,
    ) -> None:
        """Commit one signed refund anomaly while its payment aggregate is locked."""
        existing = await self._webhooks.get_by_provider_event_id(payload.provider_event_id)
        if existing is not None:
            if not compare_digest(existing.raw_body_hash, body_hash):
                raise PaymentIntegrityError(
                    "A Razorpay event ID was reused with different signed content",
                    "PAYMENT_WEBHOOK_EVENT_INTEGRITY_FAILED",
                )
            # This lookup follows an aggregate row lock, so release it before
            # Redis admission or worker return on a concurrent duplicate.
            await self._session.rollback()
            return
        webhook = RazorpayWebhookEvent(
            id=new_razorpay_webhook_event_id(),
            provider_event_id=payload.provider_event_id,
            provider_event_type=envelope.event_type,
            raw_body_hash=body_hash,
            provider_created_at=envelope.provider_created_at,
            received_at=payload.received_at,
            processed_at=self._processed_at(payload.received_at),
            processing_status=WebhookProcessingStatus.RECONCILIATION_REQUIRED,
            processing_reason_code="PAYMENT_REFUND_EVIDENCE_DETECTED",
            provider_order_id=envelope.provider_order_id,
            provider_payment_id=envelope.provider_payment_id,
            transaction_id=transaction.id,
        )
        await self._mark_locked_reconciliation_required(
            transaction,
            reason_code="PAYMENT_REFUND_EVIDENCE_DETECTED",
            actor_type=PaymentEventActorType.PROVIDER_WEBHOOK,
            actor_id=payload.provider_event_id,
            metadata={
                "provider_payment_ids": (
                    [envelope.provider_payment_id]
                    if envelope.provider_payment_id is not None
                    else []
                )
            },
            webhook_event=webhook,
            idempotency_key=f"webhook:{payload.provider_event_id}",
        )

    async def _validate_new_claim(
        self,
        authorization: Any,
        *,
        identity_subject: str,
        now: datetime,
    ) -> None:
        try:
            authorization_hash = recompute_authorization_hash(authorization)
        except (TypeError, ValueError) as error:
            raise PaymentIntegrityError(
                "Purchase authorization cannot be integrity-verified",
                "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED",
            ) from error
        if not compare_digest(authorization_hash, authorization.authorization_hash):
            raise PaymentIntegrityError(
                "Purchase authorization failed integrity verification",
                "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED",
            )
        if now >= self._as_utc(authorization.expires_at):
            raise PaymentExpiredError(
                "Purchase authorization expired before payment transaction creation",
                "PAYMENT_AUTHORIZATION_EXPIRED",
            )
        if PurchaseType(authorization.purchase_type) is not PurchaseType.ONE_TIME:
            raise PaymentConflictError(
                "Razorpay Orders currently support only one-time purchases",
                "PAYMENT_PURCHASE_TYPE_UNSUPPORTED",
            )
        if type(authorization.amount) is not int or authorization.amount <= 0:
            raise PaymentConflictError(
                "The authorized payment amount must be positive",
                "PAYMENT_AMOUNT_UNSUPPORTED",
            )

        policy = await self._policies.get(authorization.policy_id)
        quote = await self._quotes.get(authorization.quote_id)
        if policy is None or quote is None:
            raise PaymentIntegrityError(
                "Purchase authorization parent evidence is missing",
                "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED",
            )
        try:
            policy_verified = verify_policy_integrity(policy).hash_matches
            quote_verified = verify_quote_integrity(quote).hash_matches
        except IntegrityStructureError as error:
            raise PaymentIntegrityError(
                "Purchase authorization parent evidence is structurally invalid",
                "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED",
            ) from error
        if not policy_verified or not quote_verified:
            raise PaymentIntegrityError(
                "Purchase authorization parent evidence failed integrity verification",
                "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED",
            )
        if (
            policy.subject_ref != authorization.subject_ref
            or policy.subject_ref != identity_subject
        ):
            raise PaymentIntegrityError(
                "Purchase authorization policy ownership is inconsistent",
                "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED",
            )
        if (
            authorization.policy_hash != policy.policy_hash
            or authorization.quote_hash != quote.quote_hash
            or authorization.merchant_id != quote.merchant_id
            or authorization.service_id != quote.service_id
            or authorization.amount != quote.amount
            or authorization.currency != quote.currency
            or PurchaseType(authorization.purchase_type) is not PurchaseType(quote.purchase_type)
        ):
            raise PaymentIntegrityError(
                "Purchase authorization terms do not match their parent records",
                "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED",
            )
        try:
            evaluation = await self._evaluation_service.get_verified(authorization.evaluation_id)
        except Exception as error:
            raise PaymentIntegrityError(
                "Purchase authorization evaluation could not be verified",
                "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED",
            ) from error
        if (
            evaluation.decision is not PolicyDecision.ALLOW
            or evaluation.policy_id != authorization.policy_id
            or evaluation.quote_id != authorization.quote_id
            or evaluation.policy_hash != authorization.policy_hash
            or evaluation.quote_hash != authorization.quote_hash
        ):
            raise PaymentIntegrityError(
                "Only the exact ALLOW evaluation may cross the payment boundary",
                "PAYMENT_AUTHORIZATION_INTEGRITY_FAILED",
            )

        catalog_row = await self._services.get_with_merchant_for_quote(authorization.service_id)
        if catalog_row is None:
            raise PaymentConflictError(
                "The authorized service is no longer available",
                "PAYMENT_SERVICE_NOT_ACTIVE",
            )
        merchant, service = catalog_row
        if (
            merchant.id != authorization.merchant_id
            or service.merchant_id != merchant.id
            or MerchantStatus(merchant.status) is not MerchantStatus.ACTIVE
            or ServiceStatus(service.status) is not ServiceStatus.ACTIVE
        ):
            raise PaymentConflictError(
                "The authorized merchant or service is no longer active",
                "PAYMENT_SERVICE_NOT_ACTIVE",
            )

    async def _create_provider_order(self, transaction_id: str) -> PaymentTransaction:
        provider = self._require_provider()
        transaction = await self._transactions.get(transaction_id)
        if transaction is None:
            raise PaymentNotFoundError(
                f"Payment transaction '{transaction_id}' was not found",
                "PAYMENT_TRANSACTION_NOT_FOUND",
            )
        self._verify_transaction_integrity(transaction)
        request = CreateProviderOrder(
            amount=transaction.amount,
            currency=transaction.currency,
            receipt=transaction.provider_receipt,
            notes={
                "metergate_transaction_id": transaction.id,
                "authorization_id": transaction.authorization_id,
                "quote_id": transaction.quote_id,
            },
            partial_payment=False,
        )
        await self._session.commit()
        try:
            order = await provider.create_order(request)
        except PaymentProviderRejectedError:
            return await self._record_simple_transition(
                transaction.id,
                next_state=PaymentTransactionState.ORDER_CREATION_FAILED,
                event_type=PaymentTransactionEventType.RAZORPAY_ORDER_CREATION_FAILED,
                reason_code="PAYMENT_ORDER_CREATION_FAILED",
                actor_type=PaymentEventActorType.PROVIDER_API,
            )
        except (PaymentProviderTimeoutError, PaymentProviderUnavailableError):
            return await self._record_simple_transition(
                transaction.id,
                next_state=self._order_recovery_failure_state(transaction),
                event_type=PaymentTransactionEventType.RAZORPAY_ORDER_CREATION_UNCERTAIN,
                reason_code="PAYMENT_ORDER_CREATION_UNCERTAIN",
                actor_type=PaymentEventActorType.PROVIDER_API,
            )
        except PaymentProviderResponseError:
            return await self._mark_reconciliation_required(
                transaction.id,
                reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                actor_type=PaymentEventActorType.PROVIDER_API,
            )

        if not self._order_matches(transaction, order) or order.status != "created":
            return await self._mark_reconciliation_required(
                transaction.id,
                reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=order.id,
            )
        return await self._attach_provider_order(transaction.id, order)

    async def _reconcile_order_creation(self, transaction_id: str) -> PaymentTransaction:
        provider = self._require_provider()
        transaction = await self._transactions.get(transaction_id)
        if transaction is None:
            raise PaymentNotFoundError(
                f"Payment transaction '{transaction_id}' was not found",
                "PAYMENT_TRANSACTION_NOT_FOUND",
            )
        self._verify_transaction_integrity(transaction)
        if transaction.provider_order_id is not None:
            return transaction
        await self._session.commit()
        try:
            orders = await provider.find_orders_by_receipt(
                transaction.provider_receipt,
                created_from=self._as_utc(transaction.order_creation_started_at)
                - timedelta(minutes=5),
                created_to=self._read_clock() + timedelta(minutes=5),
            )
        except PaymentProviderTimeoutError as error:
            await self._record_order_recovery_problem(
                transaction,
                reason_code="PAYMENT_PROVIDER_TIMEOUT",
            )
            raise PaymentTimeoutError(
                "Razorpay receipt reconciliation timed out",
                "PAYMENT_PROVIDER_TIMEOUT",
            ) from error
        except PaymentProviderUnavailableError as error:
            await self._record_order_recovery_problem(
                transaction,
                reason_code="PAYMENT_PROVIDER_UNAVAILABLE",
            )
            raise PaymentUnavailableError(
                "Razorpay receipt reconciliation is unavailable",
                "PAYMENT_PROVIDER_UNAVAILABLE",
            ) from error
        except (PaymentProviderRejectedError, PaymentProviderResponseError):
            return await self._mark_reconciliation_required(
                transaction.id,
                reason_code="PAYMENT_RECONCILIATION_REQUIRED",
                actor_type=PaymentEventActorType.PROVIDER_API,
            )
        if len(orders) == 0:
            return await self._record_order_recovery_problem(
                transaction,
                reason_code="PAYMENT_ORDER_CREATION_UNCERTAIN",
            )
        if len(orders) != 1 or not self._order_matches(transaction, orders[0]):
            return await self._mark_reconciliation_required(
                transaction.id,
                reason_code="PAYMENT_RECONCILIATION_REQUIRED",
                actor_type=PaymentEventActorType.PROVIDER_API,
            )
        attached = await self._attach_provider_order(transaction.id, orders[0])
        if orders[0].status != "created":
            return await self._fetch_and_apply_order(
                attached.id,
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=orders[0].id,
                idempotency_key=(
                    f"receipt-reconcile:{attached.id}:{orders[0].id}:{orders[0].status}"
                ),
            )
        return attached

    async def _attach_provider_order(
        self,
        transaction_id: str,
        order: ProviderOrder,
    ) -> PaymentTransaction:
        transaction = await self._transactions.get_for_update(transaction_id)
        if transaction is None:
            raise PaymentNotFoundError(
                f"Payment transaction '{transaction_id}' was not found",
                "PAYMENT_TRANSACTION_NOT_FOUND",
            )
        self._verify_transaction_integrity(transaction)
        if transaction.provider_order_id is not None:
            if transaction.provider_order_id != order.id:
                return await self._mark_locked_reconciliation_required(
                    transaction,
                    reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                    actor_type=PaymentEventActorType.PROVIDER_API,
                    actor_id=order.id,
                )
            return transaction
        if not self._order_matches(transaction, order):
            return await self._mark_locked_reconciliation_required(
                transaction,
                reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                actor_type=PaymentEventActorType.PROVIDER_API,
                actor_id=order.id,
            )
        prior = PaymentTransactionState(transaction.transaction_state)
        transaction.provider_order_id = order.id
        transaction.provider_order_status = RazorpayOrderStatus(order.status)
        now = max(
            self._read_clock(),
            self._as_utc(transaction.order_creation_started_at),
        )
        transaction.order_created_at = now
        # A receipt-recovered Order may already be attempted or paid.  Attach its
        # immutable identity, but keep the aggregate fail-closed until the normal
        # provider-evidence path has reconciled that advanced state.  In
        # particular, never expose Checkout for an Order already reported paid.
        next_state = (
            PaymentTransactionState.ORDER_CREATED
            if order.status == "created"
            else PaymentTransactionState.RECONCILIATION_REQUIRED
        )
        transaction.transaction_state = next_state
        event = self._event(
            transaction,
            event_type=PaymentTransactionEventType.RAZORPAY_ORDER_CREATED,
            actor_type=PaymentEventActorType.PROVIDER_API,
            actor_id=order.id,
            prior_state=prior,
            resulting_state=next_state,
            reason_code=(
                "PAYMENT_ORDER_CREATED"
                if next_state is PaymentTransactionState.ORDER_CREATED
                else "PAYMENT_RECONCILIATION_REQUIRED"
            ),
            metadata={"provider_order_id": order.id, "provider_status": order.status},
            idempotency_key=f"{transaction.id}:order:{order.id}",
            occurred_at=now,
        )
        return await self._transactions.update_with_event(transaction, event=event)

    async def _fetch_and_apply_order(
        self,
        transaction_id: str,
        *,
        actor_type: PaymentEventActorType,
        actor_id: str | None,
        idempotency_key: str,
    ) -> PaymentTransaction:
        transaction = await self._transactions.get(transaction_id)
        if transaction is None or transaction.provider_order_id is None:
            raise PaymentConflictError(
                "The payment transaction has no provider order to reconcile",
                "PAYMENT_ORDER_NOT_READY",
            )
        await self._session.commit()
        provider = self._require_provider()
        try:
            order, payments = await asyncio.gather(
                provider.fetch_order(transaction.provider_order_id),
                provider.fetch_payments_for_order(transaction.provider_order_id),
            )
        except PaymentProviderTimeoutError as error:
            raise PaymentTimeoutError(
                "Razorpay reconciliation timed out",
                "PAYMENT_PROVIDER_TIMEOUT",
            ) from error
        except PaymentProviderUnavailableError as error:
            raise PaymentUnavailableError(
                "Razorpay reconciliation is unavailable",
                "PAYMENT_PROVIDER_UNAVAILABLE",
            ) from error
        except (PaymentProviderRejectedError, PaymentProviderResponseError):
            return await self._mark_reconciliation_required(
                transaction.id,
                reason_code="PAYMENT_PROVIDER_RESPONSE_MISMATCH",
                actor_type=actor_type,
                actor_id=actor_id,
            )
        return await self._apply_provider_evidence(
            transaction.id,
            order=order,
            payments=payments,
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            authoritative_snapshot=True,
        )

    async def _apply_provider_evidence(
        self,
        transaction_id: str,
        *,
        order: ProviderOrder,
        payments: Sequence[ProviderPayment],
        actor_type: PaymentEventActorType,
        actor_id: str | None,
        idempotency_key: str,
        webhook_event: RazorpayWebhookEvent | None = None,
        authoritative_snapshot: bool = False,
    ) -> PaymentTransaction:
        transaction = await self._transactions.get_for_update(transaction_id)
        if transaction is None:
            raise PaymentNotFoundError(
                f"Payment transaction '{transaction_id}' was not found",
                "PAYMENT_TRANSACTION_NOT_FOUND",
            )
        self._verify_transaction_integrity(transaction)

        evidence_fingerprint = self._provider_evidence_fingerprint(order, payments)

        async def mark_evidence_anomaly(
            reason_code: str,
            *,
            metadata: dict[str, Any] | None = None,
        ) -> PaymentTransaction:
            # Contradictory evidence must never reuse the observation event's
            # idempotency key: a previously clean observation with that key
            # would otherwise roll back the anomaly on the unique constraint.
            reconciliation_epoch = await self._reconciliation_epoch(transaction.id)
            anomaly_digest = sha256_json(
                {
                    "observation_key": idempotency_key,
                    "reason_code": reason_code,
                    "evidence_fingerprint": evidence_fingerprint,
                    "reconciliation_epoch": reconciliation_epoch,
                }
            ).removeprefix("sha256:")
            anomaly_key = f"evidence-anomaly:{transaction.id}:{anomaly_digest}"
            if await self._events.get_by_idempotency_key(anomaly_key) is not None:
                await self._session.rollback()
                current = await self._transactions.get(transaction.id)
                assert current is not None
                return current
            return await self._mark_locked_reconciliation_required(
                transaction,
                reason_code=reason_code,
                actor_type=actor_type,
                actor_id=actor_id,
                metadata=metadata,
                webhook_event=webhook_event,
                idempotency_key=anomaly_key,
            )

        if webhook_event is not None:
            existing_webhook = await self._webhooks.get_by_provider_event_id(
                webhook_event.provider_event_id
            )
            if existing_webhook is not None:
                if not compare_digest(
                    existing_webhook.raw_body_hash,
                    webhook_event.raw_body_hash,
                ):
                    raise PaymentIntegrityError(
                        "A Razorpay event ID was reused with different signed content",
                        "PAYMENT_WEBHOOK_EVENT_INTEGRITY_FAILED",
                    )
                return transaction

        if (
            webhook_event is not None
            and webhook_event.provider_event_type in _VALUE_REVOKING_WEBHOOK_EVENTS
        ):
            webhook_event.processing_status = WebhookProcessingStatus.RECONCILIATION_REQUIRED
            webhook_event.processing_reason_code = "PAYMENT_REFUND_EVIDENCE_DETECTED"
            payment_ids = (
                [webhook_event.provider_payment_id]
                if webhook_event.provider_payment_id is not None
                else []
            )
            return await mark_evidence_anomaly(
                "PAYMENT_REFUND_EVIDENCE_DETECTED",
                metadata={"provider_payment_ids": payment_ids},
            )

        mismatch = not self._order_matches(transaction, order)
        mismatch = mismatch or transaction.provider_order_id != order.id
        for payment in payments:
            mismatch = mismatch or not self._payment_matches(transaction, payment)
            if payment.order_id != order.id:
                mismatch = True
        if len({payment.id for payment in payments}) != len(payments):
            mismatch = True
        if mismatch:
            if webhook_event is not None:
                webhook_event.processing_status = WebhookProcessingStatus.RECONCILIATION_REQUIRED
                webhook_event.processing_reason_code = "PAYMENT_PROVIDER_RESPONSE_MISMATCH"
            return await mark_evidence_anomaly("PAYMENT_PROVIDER_RESPONSE_MISMATCH")

        stored_order_status = (
            RazorpayOrderStatus(transaction.provider_order_status)
            if transaction.provider_order_status is not None
            else None
        )
        incoming_order_status = RazorpayOrderStatus(order.status)
        if stored_order_status is not None and not can_transition_order_status(
            stored_order_status, incoming_order_status
        ):
            if webhook_event is not None:
                webhook_event.processing_status = WebhookProcessingStatus.RECONCILIATION_REQUIRED
                webhook_event.processing_reason_code = "PAYMENT_PROVIDER_EVIDENCE_REGRESSED"
            return await mark_evidence_anomaly(
                "PAYMENT_PROVIDER_EVIDENCE_REGRESSED",
                metadata={"evidence_kind": "order_status"},
            )

        now = self._reconciliation_time(transaction)
        changed_attempts: list[PaymentAttempt] = []
        attempt_by_payment_id: dict[str, PaymentAttempt] = {
            attempt.provider_payment_id: attempt
            for attempt in await self._attempts.list_for_transaction(transaction.id)
        }
        captured_ids = {
            attempt.provider_payment_id
            for attempt in attempt_by_payment_id.values()
            if attempt.captured
        }
        refunded_payment_ids = sorted(
            payment.id
            for payment in payments
            if payment.status == "refunded" or payment.amount_refunded > 0
        )
        if refunded_payment_ids:
            if webhook_event is not None:
                webhook_event.processing_status = WebhookProcessingStatus.RECONCILIATION_REQUIRED
                webhook_event.processing_reason_code = "PAYMENT_REFUND_EVIDENCE_DETECTED"
            return await mark_evidence_anomaly(
                "PAYMENT_REFUND_EVIDENCE_DETECTED",
                metadata={"provider_payment_ids": refunded_payment_ids},
            )
        incoming_payment_ids = {payment.id for payment in payments}
        missing_captured_ids = sorted(captured_ids - incoming_payment_ids)
        if authoritative_snapshot and missing_captured_ids:
            if webhook_event is not None:
                webhook_event.processing_status = WebhookProcessingStatus.RECONCILIATION_REQUIRED
                webhook_event.processing_reason_code = "PAYMENT_CAPTURE_EVIDENCE_MISSING"
            return await mark_evidence_anomaly(
                "PAYMENT_CAPTURE_EVIDENCE_MISSING",
                metadata={"provider_payment_ids": missing_captured_ids},
            )
        regressed_attempt_ids = sorted(
            payment.id
            for payment in payments
            if payment.id in attempt_by_payment_id
            and attempt_by_payment_id[payment.id].captured
            and not can_transition_attempt_status(
                attempt_by_payment_id[payment.id].provider_status,
                PaymentAttemptStatus(payment.status),
            )
        )
        if regressed_attempt_ids:
            if webhook_event is not None:
                webhook_event.processing_status = WebhookProcessingStatus.RECONCILIATION_REQUIRED
                webhook_event.processing_reason_code = "PAYMENT_PROVIDER_EVIDENCE_REGRESSED"
            return await mark_evidence_anomaly(
                "PAYMENT_PROVIDER_EVIDENCE_REGRESSED",
                metadata={
                    "evidence_kind": "payment_status",
                    "provider_payment_ids": regressed_attempt_ids,
                },
            )
        incoming_captured_ids = {
            payment.id
            for payment in payments
            if payment.captured or payment.status in {"captured", "refunded"}
        }
        if len(captured_ids | incoming_captured_ids) > 1:
            if webhook_event is not None:
                webhook_event.processing_status = WebhookProcessingStatus.RECONCILIATION_REQUIRED
                webhook_event.processing_reason_code = "PAYMENT_MULTIPLE_CAPTURES_DETECTED"
            return await mark_evidence_anomaly("PAYMENT_MULTIPLE_CAPTURES_DETECTED")

        evidence_attempt: PaymentAttempt | None = None
        for payment in payments:
            if payment.captured and payment.status not in {"captured", "refunded"}:
                if webhook_event is not None:
                    webhook_event.processing_status = (
                        WebhookProcessingStatus.RECONCILIATION_REQUIRED
                    )
                    webhook_event.processing_reason_code = "PAYMENT_PROVIDER_RESPONSE_MISMATCH"
                return await mark_evidence_anomaly("PAYMENT_PROVIDER_RESPONSE_MISMATCH")
            if payment.method is not None and len(payment.method) > 32:
                if webhook_event is not None:
                    webhook_event.processing_status = (
                        WebhookProcessingStatus.RECONCILIATION_REQUIRED
                    )
                    webhook_event.processing_reason_code = "PAYMENT_PROVIDER_RESPONSE_MISMATCH"
                return await mark_evidence_anomaly("PAYMENT_PROVIDER_RESPONSE_MISMATCH")
            attempt = attempt_by_payment_id.get(payment.id)
            if attempt is None:
                globally_existing = await self._attempts.get_by_provider_payment_id_for_update(
                    payment.id
                )
                if globally_existing is not None and globally_existing.transaction_id != (
                    transaction.id
                ):
                    if webhook_event is not None:
                        webhook_event.processing_status = (
                            WebhookProcessingStatus.RECONCILIATION_REQUIRED
                        )
                        webhook_event.processing_reason_code = "PAYMENT_PROVIDER_PAYMENT_MISMATCH"
                    return await mark_evidence_anomaly("PAYMENT_PROVIDER_PAYMENT_MISMATCH")
                attempt = PaymentAttempt(
                    id=new_payment_attempt_id(),
                    transaction_id=transaction.id,
                    provider=PaymentProviderName.RAZORPAY,
                    provider_order_id=order.id,
                    provider_payment_id=payment.id,
                    amount=payment.amount,
                    currency=payment.currency,
                    provider_status=PaymentAttemptStatus(payment.status),
                    method=payment.method,
                    captured=payment.captured or payment.status in {"captured", "refunded"},
                    provider_created_at=payment.created_at,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                attempt_by_payment_id[payment.id] = attempt
                changed_attempts.append(attempt)
            else:
                if (
                    attempt.transaction_id != transaction.id
                    or attempt.provider_order_id != order.id
                    or attempt.amount != payment.amount
                    or attempt.currency != payment.currency
                    or self._as_utc(attempt.provider_created_at) != payment.created_at
                ):
                    if webhook_event is not None:
                        webhook_event.processing_status = (
                            WebhookProcessingStatus.RECONCILIATION_REQUIRED
                        )
                        webhook_event.processing_reason_code = "PAYMENT_PROVIDER_PAYMENT_MISMATCH"
                    return await mark_evidence_anomaly("PAYMENT_PROVIDER_PAYMENT_MISMATCH")
                next_status = PaymentAttemptStatus(payment.status)
                if can_transition_attempt_status(attempt.provider_status, next_status):
                    attempt.provider_status = next_status
                    attempt.captured = (
                        attempt.captured
                        or payment.captured
                        or next_status
                        in {
                            PaymentAttemptStatus.CAPTURED,
                            PaymentAttemptStatus.REFUNDED,
                        }
                    )
                attempt.last_seen_at = max(self._as_utc(attempt.last_seen_at), now)
                changed_attempts.append(attempt)
            if evidence_attempt is None or attempt.captured:
                evidence_attempt = attempt

        if (
            webhook_event is None
            and await self._events.get_by_idempotency_key(idempotency_key) is not None
        ):
            await self._session.rollback()
            current = await self._transactions.get(transaction_id)
            assert current is not None
            return current

        prior_state = PaymentTransactionState(transaction.transaction_state)
        current_order_status = stored_order_status
        if can_transition_order_status(current_order_status, incoming_order_status):
            transaction.provider_order_status = incoming_order_status
        effective_order_status = RazorpayOrderStatus(transaction.provider_order_status)
        captured = any(attempt.captured for attempt in attempt_by_payment_id.values())
        authorized = any(
            PaymentAttemptStatus(attempt.provider_status) is PaymentAttemptStatus.AUTHORIZED
            for attempt in attempt_by_payment_id.values()
        )
        failed = any(
            PaymentAttemptStatus(attempt.provider_status) is PaymentAttemptStatus.FAILED
            for attempt in attempt_by_payment_id.values()
        )
        if prior_state is PaymentTransactionState.PAID:
            next_state = PaymentTransactionState.PAID
        elif captured or effective_order_status is RazorpayOrderStatus.PAID:
            next_state = PaymentTransactionState.PAID
        elif authorized:
            next_state = PaymentTransactionState.PAYMENT_AUTHORIZED
        elif attempt_by_payment_id or effective_order_status is RazorpayOrderStatus.ATTEMPTED:
            next_state = PaymentTransactionState.PAYMENT_PENDING
        else:
            next_state = PaymentTransactionState.ORDER_CREATED
        if not can_transition_transaction(prior_state, next_state):
            next_state = prior_state
        transaction.transaction_state = next_state
        transaction.last_reconciled_at = now
        if next_state is PaymentTransactionState.PAID and transaction.paid_at is None:
            transaction.paid_at = now

        event_type, reason_code = self._evidence_event_kind(
            prior_state=prior_state,
            next_state=next_state,
            captured=captured,
            order_paid=effective_order_status is RazorpayOrderStatus.PAID,
            authorized=authorized,
            failed=failed,
        )
        event_metadata: dict[str, Any] = {
            "provider_order_id": order.id,
            "provider_order_status": order.status,
            "payment_count": len(payments),
            "authoritative_snapshot": authoritative_snapshot,
        }
        if authoritative_snapshot:
            unresolved_revision = await self._unresolved_reconciliation_revision(transaction.id)
            if unresolved_revision is not None:
                event_metadata["resolved_reconciliation_revision"] = unresolved_revision
        event = self._event(
            transaction,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            prior_state=prior_state,
            resulting_state=next_state,
            reason_code=reason_code,
            metadata=event_metadata,
            payment_attempt_id=evidence_attempt.id if evidence_attempt is not None else None,
            source_webhook_event_id=(webhook_event.id if webhook_event is not None else None),
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        return await self._transactions.update_with_event(
            transaction,
            event=event,
            attempts=tuple(changed_attempts),
            webhook_event=webhook_event,
        )

    @staticmethod
    def _provider_evidence_fingerprint(
        order: ProviderOrder,
        payments: Sequence[ProviderPayment],
    ) -> str:
        """Bind idempotency decisions to every normalized trust-relevant field."""
        return sha256_json(
            {
                "order": {
                    "id": order.id,
                    "amount": order.amount,
                    "amount_paid": order.amount_paid,
                    "amount_due": order.amount_due,
                    "currency": order.currency,
                    "receipt": order.receipt,
                    "status": order.status,
                    "created_at": canonical_utc_datetime(order.created_at),
                },
                "payments": [
                    {
                        "id": payment.id,
                        "order_id": payment.order_id,
                        "amount": payment.amount,
                        "currency": payment.currency,
                        "status": payment.status,
                        "captured": payment.captured,
                        "amount_refunded": payment.amount_refunded,
                        "method": payment.method,
                        "created_at": canonical_utc_datetime(payment.created_at),
                    }
                    for payment in sorted(payments, key=lambda candidate: candidate.id)
                ],
            }
        )

    async def _unresolved_reconciliation_revision(self, transaction_id: str) -> int | None:
        """Return the latest anomaly revision not closed by trusted full-snapshot evidence."""
        unresolved: int | None = None
        requires_manual_resolution = False
        for event in await self._events.list_for_transaction(transaction_id):
            event_type = PaymentTransactionEventType(event.event_type)
            if event_type is PaymentTransactionEventType.RECONCILIATION_REQUIRED:
                unresolved = event.transaction_revision
                requires_manual_resolution = requires_manual_resolution or event.reason_code in {
                    "PAYMENT_REFUND_EVIDENCE_DETECTED",
                    "PAYMENT_MULTIPLE_CAPTURES_DETECTED",
                }
                continue
            resolved_revision = event.event_metadata.get("resolved_reconciliation_revision")
            if (
                unresolved is not None
                and not requires_manual_resolution
                and event.event_metadata.get("authoritative_snapshot") is True
                and type(resolved_revision) is int
                and resolved_revision == unresolved
            ):
                unresolved = None
        return unresolved

    async def _reconciliation_epoch(self, transaction_id: str) -> int:
        """Return the revision of the latest trusted anomaly resolution, or zero."""
        unresolved: int | None = None
        requires_manual_resolution = False
        epoch = 0
        for event in await self._events.list_for_transaction(transaction_id):
            event_type = PaymentTransactionEventType(event.event_type)
            if event_type is PaymentTransactionEventType.RECONCILIATION_REQUIRED:
                unresolved = event.transaction_revision
                requires_manual_resolution = requires_manual_resolution or event.reason_code in {
                    "PAYMENT_REFUND_EVIDENCE_DETECTED",
                    "PAYMENT_MULTIPLE_CAPTURES_DETECTED",
                }
                continue
            resolved_revision = event.event_metadata.get("resolved_reconciliation_revision")
            if (
                unresolved is not None
                and not requires_manual_resolution
                and event.event_metadata.get("authoritative_snapshot") is True
                and type(resolved_revision) is int
                and resolved_revision == unresolved
            ):
                unresolved = None
                epoch = event.transaction_revision
        return epoch

    async def _record_simple_transition(
        self,
        transaction_id: str,
        *,
        next_state: PaymentTransactionState,
        event_type: PaymentTransactionEventType,
        reason_code: str,
        actor_type: PaymentEventActorType,
        actor_id: str | None = None,
    ) -> PaymentTransaction:
        transaction = await self._transactions.get_for_update(transaction_id)
        if transaction is None:
            raise PaymentNotFoundError(
                f"Payment transaction '{transaction_id}' was not found",
                "PAYMENT_TRANSACTION_NOT_FOUND",
            )
        self._verify_transaction_integrity(transaction)
        prior = PaymentTransactionState(transaction.transaction_state)
        if prior is PaymentTransactionState.PAID:
            next_state = prior
        elif not can_transition_transaction(prior, next_state):
            return transaction
        transaction.transaction_state = next_state
        now = self._read_clock()
        event = self._event(
            transaction,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            prior_state=prior,
            resulting_state=next_state,
            reason_code=reason_code,
            metadata={},
            idempotency_key=f"{transaction.id}:{reason_code}:{transaction.revision + 1}",
            occurred_at=now,
        )
        return await self._transactions.update_with_event(transaction, event=event)

    async def _record_order_recovery_problem(
        self,
        transaction: PaymentTransaction,
        *,
        reason_code: str,
    ) -> PaymentTransaction:
        """Record receipt recovery failure without weakening a fail-closed state."""
        state = PaymentTransactionState(transaction.transaction_state)
        if state in {
            PaymentTransactionState.ORDER_CREATION_PENDING,
            PaymentTransactionState.ORDER_CREATION_UNCERTAIN,
        }:
            next_state = PaymentTransactionState.ORDER_CREATION_UNCERTAIN
            event_type = PaymentTransactionEventType.RAZORPAY_ORDER_CREATION_UNCERTAIN
        elif state is PaymentTransactionState.RECONCILIATION_REQUIRED:
            next_state = state
            event_type = PaymentTransactionEventType.RECONCILIATION_REQUIRED
        else:
            next_state = state
            event_type = PaymentTransactionEventType.PAYMENT_RECONCILED
        return await self._record_simple_transition(
            transaction.id,
            next_state=next_state,
            event_type=event_type,
            reason_code=reason_code,
            actor_type=PaymentEventActorType.PROVIDER_API,
        )

    async def _mark_reconciliation_required(
        self,
        transaction_id: str,
        *,
        reason_code: str,
        actor_type: PaymentEventActorType,
        actor_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PaymentTransaction:
        transaction = await self._transactions.get_for_update(transaction_id)
        if transaction is None:
            raise PaymentNotFoundError(
                f"Payment transaction '{transaction_id}' was not found",
                "PAYMENT_TRANSACTION_NOT_FOUND",
            )
        return await self._mark_locked_reconciliation_required(
            transaction,
            reason_code=reason_code,
            actor_type=actor_type,
            actor_id=actor_id,
            metadata=metadata,
        )

    async def _mark_locked_reconciliation_required(
        self,
        transaction: PaymentTransaction,
        *,
        reason_code: str,
        actor_type: PaymentEventActorType,
        actor_id: str | None,
        metadata: dict[str, Any] | None = None,
        webhook_event: RazorpayWebhookEvent | None = None,
        idempotency_key: str | None = None,
    ) -> PaymentTransaction:
        self._verify_transaction_integrity(transaction)
        prior = PaymentTransactionState(transaction.transaction_state)
        next_state = (
            PaymentTransactionState.PAID
            if prior is PaymentTransactionState.PAID
            else PaymentTransactionState.RECONCILIATION_REQUIRED
        )
        transaction.transaction_state = next_state
        now = self._reconciliation_time(transaction)
        transaction.last_reconciled_at = now
        event = self._event(
            transaction,
            event_type=PaymentTransactionEventType.RECONCILIATION_REQUIRED,
            actor_type=actor_type,
            actor_id=actor_id,
            prior_state=prior,
            resulting_state=next_state,
            reason_code=reason_code,
            metadata=metadata or {},
            source_webhook_event_id=(webhook_event.id if webhook_event is not None else None),
            idempotency_key=idempotency_key
            or f"{transaction.id}:{reason_code}:{transaction.revision + 1}",
            occurred_at=now,
        )
        return await self._transactions.update_with_event(
            transaction,
            event=event,
            webhook_event=webhook_event,
        )

    async def _persist_unmatched_webhook(
        self,
        payload: VerifiedWebhookPayload,
        *,
        body_hash: str,
        event_type: str,
        provider_created_at: datetime,
        reason_code: str,
        status: WebhookProcessingStatus,
        provider_order_id: str | None = None,
        provider_payment_id: str | None = None,
    ) -> None:
        event = RazorpayWebhookEvent(
            id=new_razorpay_webhook_event_id(),
            provider_event_id=payload.provider_event_id,
            provider_event_type=event_type,
            raw_body_hash=body_hash,
            provider_created_at=provider_created_at,
            received_at=payload.received_at,
            processed_at=self._processed_at(payload.received_at),
            processing_status=status,
            processing_reason_code=reason_code,
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            transaction_id=None,
        )
        try:
            await self._webhooks.create(event)
        except IntegrityError:
            await self._session.rollback()
            existing = await self._webhooks.get_by_provider_event_id(payload.provider_event_id)
            if existing is None or not compare_digest(existing.raw_body_hash, body_hash):
                raise PaymentIntegrityError(
                    "A Razorpay event ID was reused with different signed content",
                    "PAYMENT_WEBHOOK_EVENT_INTEGRITY_FAILED",
                ) from None

    async def _get_owned_transaction(
        self,
        transaction_id: str,
        *,
        account_id: str,
    ) -> PaymentTransaction:
        transaction = await self._transactions.get(transaction_id)
        if transaction is None:
            raise PaymentNotFoundError(
                f"Payment transaction '{transaction_id}' was not found",
                "PAYMENT_TRANSACTION_NOT_FOUND",
            )
        self._require_transaction_owner(transaction, account_id)
        self._verify_transaction_integrity(transaction)
        return transaction

    async def _response(self, transaction: PaymentTransaction) -> PaymentTransactionResponse:
        self._verify_transaction_integrity(transaction)
        quote = await self._quotes.get(transaction.quote_id)
        if quote is None:
            raise PaymentIntegrityError(
                "Payment transaction quote evidence is missing",
                "PAYMENT_TRANSACTION_INTEGRITY_FAILED",
            )
        try:
            quote_verified = verify_quote_integrity(quote).hash_matches
            merchant_data = quote.service_snapshot["merchant"]
            service_data = quote.service_snapshot["service"]
            merchant_name = merchant_data["name"]
            service_name = service_data["name"]
        except (IntegrityStructureError, KeyError, TypeError) as error:
            raise PaymentIntegrityError(
                "Payment transaction quote evidence is invalid",
                "PAYMENT_TRANSACTION_INTEGRITY_FAILED",
            ) from error
        if (
            not quote_verified
            or quote.quote_hash != transaction.quote_hash
            or quote.merchant_id != transaction.merchant_id
            or quote.service_id != transaction.service_id
            or quote.amount != transaction.amount
            or quote.currency != transaction.currency
            or not isinstance(merchant_name, str)
            or not isinstance(service_name, str)
        ):
            raise PaymentIntegrityError(
                "Payment transaction quote binding is invalid",
                "PAYMENT_TRANSACTION_INTEGRITY_FAILED",
            )
        attempts = await self._attempts.list_for_transaction(transaction.id)
        state = PaymentTransactionState(transaction.transaction_state)
        checkout: RazorpayCheckoutConfiguration | None = None
        if (
            self._settings.payments_enabled
            and transaction.provider_order_id is not None
            and RazorpayOrderStatus(transaction.provider_order_status)
            is not RazorpayOrderStatus.PAID
            and state
            in {
                PaymentTransactionState.ORDER_CREATED,
                PaymentTransactionState.PAYMENT_PENDING,
            }
        ):
            checkout = RazorpayCheckoutConfiguration(
                transaction_id=transaction.id,
                key_id=self._key_id(),
                order_id=transaction.provider_order_id,
                amount=transaction.amount,
                currency=transaction.currency,
                name=merchant_name,
                description=service_name,
            )
        return PaymentTransactionResponse(
            reason_code=self._reason_for_state(state),
            transaction_id=transaction.id,
            state=state,
            merchant=AuthorizationParty(id=transaction.merchant_id, name=merchant_name),
            service=AuthorizationParty(id=transaction.service_id, name=service_name),
            amount=transaction.amount,
            currency=transaction.currency,
            purchase_type=transaction.purchase_type,
            authorization_id=transaction.authorization_id,
            provider="razorpay",
            provider_order_id=transaction.provider_order_id,
            provider_order_status=(
                RazorpayOrderStatus(transaction.provider_order_status).value
                if transaction.provider_order_status is not None
                else None
            ),
            attempts=[
                PaymentAttemptResponse(
                    id=attempt.id,
                    provider_payment_id=attempt.provider_payment_id,
                    status=PaymentAttemptStatus(attempt.provider_status),
                    amount=attempt.amount,
                    currency=attempt.currency,
                    captured=attempt.captured,
                    method=attempt.method,
                    provider_created_at=attempt.provider_created_at,
                    first_seen_at=attempt.first_seen_at,
                    last_seen_at=attempt.last_seen_at,
                )
                for attempt in attempts
            ],
            checkout=checkout,
            created_at=transaction.created_at,
            paid_at=transaction.paid_at,
            last_reconciled_at=transaction.last_reconciled_at,
        )

    def _event(
        self,
        transaction: PaymentTransaction,
        *,
        event_type: PaymentTransactionEventType,
        actor_type: PaymentEventActorType,
        actor_id: str | None,
        prior_state: PaymentTransactionState | None,
        resulting_state: PaymentTransactionState,
        reason_code: str,
        metadata: dict[str, Any],
        idempotency_key: str,
        occurred_at: datetime,
        revision: int | None = None,
        payment_attempt_id: str | None = None,
        source_webhook_event_id: str | None = None,
    ) -> PaymentTransactionEvent:
        return PaymentTransactionEvent(
            id=new_payment_transaction_event_id(),
            transaction_id=transaction.id,
            transaction_revision=revision or transaction.revision + 1,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            prior_state=prior_state,
            resulting_state=resulting_state,
            reason_code=reason_code,
            event_metadata=metadata,
            payment_attempt_id=payment_attempt_id,
            source_webhook_event_id=source_webhook_event_id,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _parse_webhook_envelope(raw_body: bytes) -> _WebhookEnvelope:
        try:
            raw = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError("Webhook JSON is invalid") from error
        if not isinstance(raw, Mapping) or raw.get("entity") != "event":
            raise ValueError("Webhook envelope is invalid")
        event_type = raw.get("event")
        created_at = raw.get("created_at")
        validate_provider_event_type(event_type)  # type: ignore[arg-type]
        if type(created_at) is not int or not 0 <= created_at <= (1 << 53) - 1:
            raise ValueError("Webhook event metadata is invalid")
        try:
            provider_created_at = datetime.fromtimestamp(created_at, tz=UTC)
        except (OSError, OverflowError, ValueError) as error:
            raise ValueError("Webhook timestamp is invalid") from error

        body_payload = raw.get("payload")
        if body_payload is None:
            body_payload = {}
        if not isinstance(body_payload, Mapping):
            raise ValueError("Webhook payload is invalid")
        payment = PaymentApplicationService._webhook_entity(body_payload, "payment")
        order = PaymentApplicationService._webhook_entity(body_payload, "order")
        refund = PaymentApplicationService._webhook_entity(body_payload, "refund")
        payment_id = payment.get("id") if payment is not None else None
        payment_order_id = payment.get("order_id") if payment is not None else None
        refund_payment_id = refund.get("payment_id") if refund is not None else None
        order_id = order.get("id") if order is not None else None
        if payment_id is not None:
            validate_provider_payment_id(payment_id)
        if payment_order_id is not None:
            validate_provider_order_id(payment_order_id)
        if refund_payment_id is not None:
            validate_provider_payment_id(refund_payment_id)
        if order_id is not None:
            validate_provider_order_id(order_id)
        if payment_order_id is not None and order_id is not None and payment_order_id != order_id:
            raise ValueError("Webhook entities reference different orders")
        if event_type.startswith("refund.") and (
            payment_id is None or refund_payment_id is None or payment_id != refund_payment_id
        ):
            raise ValueError("Refund webhook entities reference different payments")
        return _WebhookEnvelope(
            event_type=event_type,
            provider_created_at=provider_created_at,
            provider_order_id=payment_order_id or order_id,
            provider_payment_id=payment_id or refund_payment_id,
        )

    @staticmethod
    def _webhook_entity(
        payload: Mapping[str, object],
        name: str,
    ) -> Mapping[str, Any] | None:
        wrapper = payload.get(name)
        if wrapper is None:
            return None
        if not isinstance(wrapper, Mapping) or set(wrapper) != {"entity"}:
            raise ValueError("Webhook entity wrapper is invalid")
        entity = wrapper["entity"]
        if not isinstance(entity, Mapping):
            raise ValueError("Webhook entity is invalid")
        return entity

    @staticmethod
    def _order_matches(transaction: PaymentTransaction, order: ProviderOrder) -> bool:
        return (
            order.receipt == transaction.provider_receipt
            and order.amount == transaction.amount
            and order.currency == transaction.currency
        )

    @classmethod
    def _validate_value_release_evidence(
        cls,
        transaction: PaymentTransaction,
        *,
        order: ProviderOrder,
        payments: Sequence[ProviderPayment],
    ) -> ProviderPayment | None:
        """Return the sole unrefunded capture only for a fully paid exact Order."""
        if (
            transaction.provider_order_id is None
            or order.id != transaction.provider_order_id
            or not cls._order_matches(transaction, order)
            or order.status != "paid"
            or order.amount_paid != transaction.amount
            or order.amount_due != 0
            or not payments
            or len({payment.id for payment in payments}) != len(payments)
        ):
            return None
        if any(not cls._payment_matches(transaction, payment) for payment in payments):
            return None
        captures = [
            payment
            for payment in payments
            if payment.status == "captured" and payment.captured and payment.amount_refunded == 0
        ]
        if len(captures) != 1:
            return None
        # Any refund evidence is an unresolved contradiction even when a separate
        # captured payment remains in the order response.
        if any(
            payment.status == "refunded" or payment.amount_refunded != 0 for payment in payments
        ):
            return None
        return captures[0]

    @staticmethod
    def _payment_matches(
        transaction: PaymentTransaction,
        payment: ProviderPayment,
    ) -> bool:
        return (
            transaction.provider_order_id is not None
            and payment.order_id == transaction.provider_order_id
            and payment.amount == transaction.amount
            and payment.currency == transaction.currency
        )

    @staticmethod
    def _evidence_event_kind(
        *,
        prior_state: PaymentTransactionState,
        next_state: PaymentTransactionState,
        captured: bool,
        order_paid: bool,
        authorized: bool,
        failed: bool,
    ) -> tuple[PaymentTransactionEventType, str]:
        if prior_state is PaymentTransactionState.PAID:
            return PaymentTransactionEventType.PAYMENT_RECONCILED, "PAYMENT_RECONCILED"
        if next_state is PaymentTransactionState.PAID:
            if captured:
                return (
                    PaymentTransactionEventType.PAYMENT_CAPTURED,
                    "PAYMENT_CAPTURED",
                )
            if order_paid:
                return PaymentTransactionEventType.ORDER_PAID, "PAYMENT_CAPTURED"
        if authorized:
            return PaymentTransactionEventType.PAYMENT_AUTHORIZED, "PAYMENT_AUTHORIZED"
        if failed:
            return PaymentTransactionEventType.PAYMENT_ATTEMPT_FAILED, "PAYMENT_ATTEMPT_FAILED"
        return PaymentTransactionEventType.PAYMENT_RECONCILED, "PAYMENT_PENDING"

    @staticmethod
    def _reason_for_state(state: PaymentTransactionState) -> str:
        return {
            PaymentTransactionState.ORDER_CREATION_PENDING: "PAYMENT_ORDER_CREATION_PENDING",
            PaymentTransactionState.ORDER_CREATED: "PAYMENT_CHECKOUT_READY",
            PaymentTransactionState.ORDER_CREATION_FAILED: "PAYMENT_ORDER_CREATION_FAILED",
            PaymentTransactionState.ORDER_CREATION_UNCERTAIN: ("PAYMENT_ORDER_CREATION_UNCERTAIN"),
            PaymentTransactionState.PAYMENT_PENDING: "PAYMENT_PENDING",
            PaymentTransactionState.PAYMENT_AUTHORIZED: "PAYMENT_AUTHORIZED",
            PaymentTransactionState.PAID: "PAYMENT_CAPTURED",
            PaymentTransactionState.RECONCILIATION_REQUIRED: ("PAYMENT_RECONCILIATION_REQUIRED"),
        }[state]

    @staticmethod
    def _pending_status(transaction: PaymentTransaction) -> int:
        state = PaymentTransactionState(transaction.transaction_state)
        return (
            200
            if state in {PaymentTransactionState.ORDER_CREATED, PaymentTransactionState.PAID}
            else 202
        )

    def _should_reconcile_order_creation(self, transaction: PaymentTransaction) -> bool:
        state = PaymentTransactionState(transaction.transaction_state)
        if state is PaymentTransactionState.ORDER_CREATION_UNCERTAIN:
            return True
        if state is not PaymentTransactionState.ORDER_CREATION_PENDING:
            return False
        age = self._read_clock() - self._as_utc(transaction.order_creation_started_at)
        return age >= timedelta(seconds=self._settings.razorpay_order_recovery_age_seconds)

    @staticmethod
    def _order_recovery_failure_state(
        transaction: PaymentTransaction,
    ) -> PaymentTransactionState:
        state = PaymentTransactionState(transaction.transaction_state)
        return (
            PaymentTransactionState.ORDER_CREATION_UNCERTAIN
            if state is PaymentTransactionState.ORDER_CREATION_PENDING
            else state
        )

    @staticmethod
    def _require_transaction_owner(
        transaction: PaymentTransaction,
        account_id: str,
    ) -> None:
        if transaction.account_id != account_id:
            raise AuthenticationForbiddenError(
                "The authenticated account does not own this payment transaction",
                "AUTH_RESOURCE_OWNERSHIP_MISMATCH",
            )

    @staticmethod
    def _verify_transaction_integrity(transaction: PaymentTransaction) -> None:
        try:
            actual = recompute_payment_binding_hash(transaction)
        except (TypeError, ValueError) as error:
            raise PaymentIntegrityError(
                "Payment transaction binding is structurally invalid",
                "PAYMENT_TRANSACTION_INTEGRITY_FAILED",
            ) from error
        if not compare_digest(actual, transaction.payment_binding_hash):
            raise PaymentIntegrityError(
                "Payment transaction binding failed integrity verification",
                "PAYMENT_TRANSACTION_INTEGRITY_FAILED",
            )

    def _require_enabled(self) -> None:
        if not self._settings.payments_enabled:
            raise PaymentUnavailableError(
                "Razorpay Test Mode payments are disabled",
                "PAYMENT_DISABLED",
            )

    def _require_provider(self) -> PaymentProvider:
        if self._provider is None:
            raise PaymentUnavailableError(
                "Razorpay Test Mode provider is unavailable",
                "PAYMENT_PROVIDER_UNAVAILABLE",
            )
        return self._provider

    def _key_id(self) -> str:
        if self._settings.razorpay_key_id is None:
            raise PaymentUnavailableError(
                "Razorpay Test Mode key is unavailable",
                "PAYMENT_DISABLED",
            )
        return self._settings.razorpay_key_id.get_secret_value()

    def _key_secret(self) -> str:
        if self._settings.razorpay_key_secret is None:
            raise PaymentUnavailableError(
                "Razorpay Test Mode secret is unavailable",
                "PAYMENT_DISABLED",
            )
        return self._settings.razorpay_key_secret.get_secret_value()

    def _read_clock(self) -> datetime:
        return self._as_utc(self._clock())

    def _processed_at(self, received_at: datetime) -> datetime:
        return max(self._read_clock(), self._as_utc(received_at))

    def _reconciliation_time(self, transaction: PaymentTransaction) -> datetime:
        values = [
            self._read_clock(),
            self._as_utc(transaction.order_creation_started_at),
        ]
        if transaction.last_reconciled_at is not None:
            values.append(self._as_utc(transaction.last_reconciled_at))
        return max(values)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Payment timestamps must be timezone-aware")
        return value.astimezone(UTC)


class DatabaseWebhookEventProcessor:
    """Give every Redis delivery an isolated request-like database session."""

    def __init__(
        self,
        database: Database,
        provider: PaymentProvider,
        settings: Settings,
    ) -> None:
        self._database = database
        self._provider = provider
        self._settings = settings

    async def process_webhook(self, payload: VerifiedWebhookPayload) -> None:
        async with self._database.session() as session:
            service = PaymentApplicationService(session, self._provider, self._settings)
            await service.process_webhook(payload)


def build_razorpay_payment_provider(settings: Settings) -> PaymentProvider | None:
    """Construct the SDK adapter only when strict Test Mode support is enabled."""
    if not settings.payments_enabled:
        return None
    assert settings.razorpay_key_id is not None
    assert settings.razorpay_key_secret is not None
    return RazorpayPaymentProvider(
        key_id=settings.razorpay_key_id.get_secret_value(),
        key_secret=settings.razorpay_key_secret.get_secret_value(),
        connect_timeout_seconds=settings.razorpay_connect_timeout_seconds,
        read_timeout_seconds=settings.razorpay_read_timeout_seconds,
        max_concurrency=settings.razorpay_provider_max_concurrency,
    )


@asynccontextmanager
async def build_razorpay_webhook_worker_runtime() -> AsyncIterator[RazorpayWebhookWorker]:
    """Build and close the standalone worker's database, Redis, and SDK runtime."""
    settings = get_settings()
    if not settings.payments_enabled:
        raise RuntimeError("Razorpay Test Mode payments are disabled")
    provider = build_razorpay_payment_provider(settings)
    assert provider is not None
    database = create_database(settings)
    redis_client = Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=False,
    )
    queue = RedisPaymentWebhookQueue(redis_client)
    processor = DatabaseWebhookEventProcessor(database, provider, settings)
    consumer_name = f"metergate-{socket.gethostname()}-{os.getpid()}"
    worker = RazorpayWebhookWorker(
        queue=queue,
        processor=processor,
        consumer_name=consumer_name[:128],
    )
    try:
        yield worker
    finally:
        try:
            await redis_client.aclose()
        finally:
            await database.dispose()
