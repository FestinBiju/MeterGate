"""Deterministic compensation decisions and idempotent Razorpay refund recovery."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from hmac import compare_digest

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.payment_webhooks import VerifiedWebhookPayload
from app.core.config import Settings
from app.domain.compensation_hashing import (
    FAILURE_EVIDENCE_VERSION,
    recompute_failure_evidence_hash,
)
from app.domain.compensation_policy import evaluate_compensation_policy
from app.domain.enums import (
    CompensationDecisionState,
    CompensationEventActorType,
    CompensationEventType,
    PaymentAttemptStatus,
    PaymentProvider,
    PaymentRefundState,
    PaymentTransactionState,
    RefundOutboxEventType,
)
from app.domain.exceptions import (
    CompensationConflictError,
    CompensationForbiddenError,
    CompensationIntegrityError,
    CompensationNotFoundError,
    CompensationTimeoutError,
    CompensationUnavailableError,
)
from app.domain.ids import (
    new_compensation_case_id,
    new_compensation_event_id,
    new_payment_refund_id,
    new_refund_outbox_event_id,
)
from app.domain.refund_webhooks import parse_refund_webhook
from app.models import (
    CompensationCase,
    CompensationEvent,
    FulfillmentExecution,
    PaymentRefund,
    RefundDispatchAttempt,
    RefundOutboxEvent,
)
from app.providers import (
    CreateProviderRefund,
    PaymentProviderRejectedError,
    PaymentProviderResponseError,
    PaymentProviderTimeoutError,
    PaymentProviderUnavailableError,
    ProviderRefund,
    RefundProvider,
)
from app.repositories import (
    CompensationCaseRepository,
    CompensationEventRepository,
    EntitlementRepository,
    FulfillmentExecutionRepository,
    PaymentAttemptRepository,
    PaymentRefundRepository,
    PaymentTransactionRepository,
    QuoteRepository,
)
from app.schemas.compensations import (
    CommerceOutcome,
    CompensationCaseResponse,
    CompensationSummary,
    PaymentRefundResponse,
    RefundSummary,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RefundExecutionResult:
    """Outcome of one worker pass over a durable refund request."""

    refund_id: str
    state: PaymentRefundState
    terminal: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class TransactionRecoveryProjection:
    compensation_summary: CompensationSummary | None
    refund_summary: RefundSummary | None
    commerce_outcome: CommerceOutcome


class CompensationApplicationService:
    """Create and expose immutable failure-to-remediation evidence."""

    def __init__(self, session: AsyncSession, *, clock=utc_now) -> None:
        self._session = session
        self._clock = clock
        self._cases = CompensationCaseRepository(session)
        self._events = CompensationEventRepository(session)
        self._transactions = PaymentTransactionRepository(session)
        self._attempts = PaymentAttemptRepository(session)
        self._entitlements = EntitlementRepository(session)
        self._executions = FulfillmentExecutionRepository(session)
        self._quotes = QuoteRepository(session)
        self._refunds = PaymentRefundRepository(session)

    async def stage_for_permanent_failure(
        self,
        execution: FulfillmentExecution,
        *,
        occurred_at: datetime | None = None,
    ) -> CompensationCase | None:
        """Stage one decision beside the fulfillment's terminal commit.

        This method deliberately does not commit.  The fulfillment repository's
        existing aggregate commit makes the terminal failure, case, audit events,
        and refund outbox visible atomically.
        """
        # Fulfillment repositories own the revision increment.  Suppress an
        # early autoflush while reading the other immutable evidence, otherwise
        # SQLAlchemy's update guard would observe the terminal state before that
        # repository has advanced the execution revision.
        with getattr(self._session, "no_autoflush", nullcontext()):
            existing = await self._cases.get_by_execution_id(execution.id)
            if existing is not None:
                return existing

            transaction = await self._transactions.get(execution.transaction_id)
            entitlement = await self._entitlements.get(execution.entitlement_id)
            if transaction is None or entitlement is None:
                raise CompensationIntegrityError(
                    "Permanent fulfillment evidence is missing its commerce bindings",
                    "COMPENSATION_EVIDENCE_MISSING",
                )
            quote = await self._quotes.get(transaction.quote_id)
            if quote is None:
                raise CompensationIntegrityError(
                    "Permanent fulfillment evidence is missing its immutable quote",
                    "COMPENSATION_QUOTE_EVIDENCE_MISSING",
                )
            captured_attempts = [
                attempt
                for attempt in await self._attempts.list_for_transaction(transaction.id)
                if attempt.captured
            ]
        if not captured_attempts:
            return None
        if len(captured_attempts) != 1:
            raise CompensationIntegrityError(
                "Payment evidence contains more than one captured attempt",
                "COMPENSATION_CAPTURE_EVIDENCE_CONFLICT",
            )
        payment_attempt = captured_attempts[0]
        decision = evaluate_compensation_policy(
            transaction=transaction,
            payment_attempt=payment_attempt,
            entitlement=entitlement,
            fulfillment=execution,
            quote=quote,
        )
        if not decision.create_case:
            return None
        if decision.decision_state is None or decision.decision_provenance is None:
            raise CompensationIntegrityError(
                "Compensation policy returned an incomplete persisted decision",
                "COMPENSATION_DECISION_INVALID",
            )

        now = self._as_utc(occurred_at or self._clock())
        try:
            failure_hash = recompute_failure_evidence_hash(execution)
        except (TypeError, ValueError) as error:
            raise CompensationIntegrityError(
                "Permanent fulfillment failure evidence cannot be hashed",
                "COMPENSATION_FAILURE_EVIDENCE_INVALID",
            ) from error
        case = CompensationCase(
            id=new_compensation_case_id(),
            account_id=transaction.account_id,
            transaction_id=transaction.id,
            payment_attempt_id=payment_attempt.id,
            entitlement_id=entitlement.id,
            fulfillment_execution_id=execution.id,
            quote_id=quote.id,
            quote_hash=quote.quote_hash,
            merchant_id=transaction.merchant_id,
            service_id=transaction.service_id,
            amount_paid=transaction.amount,
            currency=transaction.currency,
            failure_code=execution.failure_code,
            failure_evidence_version=FAILURE_EVIDENCE_VERSION,
            failure_evidence_hash=failure_hash,
            recommended_action=decision.recommended_action,
            decision_state=decision.decision_state,
            decision_provenance=decision.decision_provenance,
            approved_refund_amount=decision.approved_refund_amount,
            decision_reason_code=decision.reason_code.value,
            created_at=now,
            decided_at=now,
            closed_at=None,
            revision=1,
        )
        events = [
            self._event(
                case,
                sequence=1,
                event_type=CompensationEventType.COMPENSATION_CASE_CREATED,
                reason_code="COMPENSATION_CASE_CREATED",
                idempotency_key=f"case-created:{case.id}",
                occurred_at=now,
                metadata={
                    "failure_evidence_hash": failure_hash,
                    "fulfillment_execution_id": execution.id,
                },
            ),
            self._event(
                case,
                sequence=2,
                event_type=CompensationEventType.COMPENSATION_RECOMMENDED,
                reason_code=decision.reason_code.value,
                idempotency_key=f"case-recommended:{case.id}",
                occurred_at=now,
                metadata={"recommended_action": decision.recommended_action.value},
            ),
        ]
        outbox: RefundOutboxEvent | None = None
        if decision.decision_state is CompensationDecisionState.APPROVED:
            events.append(
                self._event(
                    case,
                    sequence=3,
                    event_type=CompensationEventType.COMPENSATION_APPROVED,
                    reason_code=decision.reason_code.value,
                    idempotency_key=f"case-approved:{case.id}",
                    occurred_at=now,
                    metadata={
                        "approved_refund_amount": decision.approved_refund_amount,
                        "decision_provenance": decision.decision_provenance.value,
                    },
                )
            )
            outbox = RefundOutboxEvent(
                id=new_refund_outbox_event_id(),
                event_type=RefundOutboxEventType.REFUND_REQUESTED,
                compensation_case_id=case.id,
                deduplication_key=f"refund:{case.id}",
                payload_version="1",
                payload={
                    "compensation_case_id": case.id,
                    "transaction_id": transaction.id,
                    "approved_refund_amount": decision.approved_refund_amount,
                    "currency": transaction.currency,
                },
                created_at=now,
                available_at=now,
                processing_started_at=None,
                processed_at=None,
                attempt_count=0,
                lease_generation=0,
                lease_expires_at=None,
                last_error_code=None,
            )
            events.append(
                self._event(
                    case,
                    sequence=4,
                    event_type=CompensationEventType.REFUND_OUTBOX_CREATED,
                    reason_code="REFUND_REQUESTED",
                    idempotency_key=f"refund-outbox-created:{case.id}",
                    occurred_at=now,
                    metadata={"refund_outbox_event_id": outbox.id},
                )
            )
        self._cases.stage(case, events=events, refund_outbox_event=outbox)
        return case

    async def get_for_transaction(
        self, transaction_id: str, *, account_id: str
    ) -> CompensationCaseResponse:
        transaction = await self._transactions.get(transaction_id)
        if transaction is None:
            raise CompensationNotFoundError(
                "Payment transaction was not found", "PAYMENT_TRANSACTION_NOT_FOUND"
            )
        self._require_owner(transaction.account_id, account_id)
        case = await self._cases.get_by_transaction_id(transaction_id)
        if case is None:
            raise CompensationNotFoundError(
                "No compensation case exists for this transaction",
                "COMPENSATION_NOT_FOUND",
            )
        return await self._case_response(case)

    async def get_case(self, compensation_id: str, *, account_id: str) -> CompensationCaseResponse:
        case = await self._cases.get(compensation_id)
        if case is None:
            raise CompensationNotFoundError(
                "Compensation case was not found", "COMPENSATION_NOT_FOUND"
            )
        self._require_owner(case.account_id, account_id)
        return await self._case_response(case)

    async def get_refund(self, refund_id: str, *, account_id: str) -> PaymentRefundResponse:
        refund = await self._refunds.get(refund_id)
        if refund is None:
            raise CompensationNotFoundError("Refund was not found", "REFUND_NOT_FOUND")
        case = await self._cases.get(refund.compensation_case_id)
        if case is None:
            raise CompensationIntegrityError(
                "Refund compensation evidence is missing", "REFUND_CASE_EVIDENCE_MISSING"
            )
        self._require_owner(case.account_id, account_id)
        return payment_refund_response(refund)

    async def has_quarantine(self, transaction_id: str) -> bool:
        """Any compensation case permanently quarantines undelivered value."""
        if await self._cases.get_by_transaction_id(transaction_id) is not None:
            return True
        execution = await self._session.scalar(
            select(FulfillmentExecution).where(
                FulfillmentExecution.transaction_id == transaction_id,
                FulfillmentExecution.compensation_required.is_(True),
            )
        )
        return execution is not None

    async def projection_for_transaction(
        self,
        transaction_id: str,
        *,
        payment_state: PaymentTransactionState | str,
    ) -> TransactionRecoveryProjection:
        case = await self._cases.get_by_transaction_id(transaction_id)
        refund = (
            await self._refunds.get_by_compensation_case_id(case.id) if case is not None else None
        )
        execution = await self._session.scalar(
            select(FulfillmentExecution).where(
                FulfillmentExecution.transaction_id == transaction_id
            )
        )
        outcome: CommerceOutcome
        if PaymentTransactionState(payment_state) is not PaymentTransactionState.PAID:
            outcome = "payment_pending"
        elif case is not None:
            if (
                refund is not None
                and effective_refund_state(refund) is PaymentRefundState.REFUNDED
                and CompensationDecisionState(case.decision_state)
                is CompensationDecisionState.COMPLETED
            ):
                outcome = "refunded"
            elif refund is not None and refund.reconciliation_required_at is not None:
                outcome = "manual_review"
            elif CompensationDecisionState(case.decision_state) in {
                CompensationDecisionState.MANUAL_REVIEW,
                CompensationDecisionState.REJECTED,
            }:
                outcome = "manual_review"
            else:
                outcome = "compensation_pending"
        elif execution is not None and execution.execution_state == "succeeded":
            outcome = "fulfilled"
        elif execution is not None:
            outcome = "fulfillment_pending"
        else:
            outcome = "paid"
        return TransactionRecoveryProjection(
            compensation_summary=(compensation_summary(case) if case is not None else None),
            refund_summary=(refund_summary(refund) if refund is not None else None),
            commerce_outcome=outcome,
        )

    async def _case_response(self, case: CompensationCase) -> CompensationCaseResponse:
        refund = await self._refunds.get_by_compensation_case_id(case.id)
        attempt = await self._attempts.get(case.payment_attempt_id)
        if attempt is None or attempt.provider_payment_id is None:
            raise CompensationIntegrityError(
                "Compensation payment evidence is missing", "COMPENSATION_PAYMENT_MISSING"
            )
        return CompensationCaseResponse(
            reason_code=(
                refund_reason_code(refund)
                if refund is not None and refund.reconciliation_required_at is not None
                else case_reason_code(case)
            ),
            compensation_id=case.id,
            transaction_id=case.transaction_id,
            payment_attempt_id=case.payment_attempt_id,
            provider_payment_id=attempt.provider_payment_id,
            entitlement_id=case.entitlement_id,
            fulfillment_execution_id=case.fulfillment_execution_id,
            merchant_id=case.merchant_id,
            service_id=case.service_id,
            amount_paid=case.amount_paid,
            currency=case.currency,
            failure_code=case.failure_code,
            failure_evidence_hash=case.failure_evidence_hash,
            recommended_action=case.recommended_action.value,
            decision_state=case.decision_state.value,
            decision_provenance=(
                case.decision_provenance.value if case.decision_provenance is not None else None
            ),
            approved_refund_amount=case.approved_refund_amount,
            decision_reason_code=case.decision_reason_code,
            created_at=case.created_at,
            decided_at=case.decided_at,
            closed_at=case.closed_at,
            refund=(refund_summary(refund) if refund is not None else None),
        )

    @staticmethod
    def _event(
        case: CompensationCase,
        *,
        sequence: int,
        event_type: CompensationEventType,
        reason_code: str,
        idempotency_key: str,
        occurred_at: datetime,
        metadata: dict[str, object],
    ) -> CompensationEvent:
        return CompensationEvent(
            id=new_compensation_event_id(),
            compensation_case_id=case.id,
            transaction_id=case.transaction_id,
            payment_refund_id=None,
            sequence=sequence,
            case_revision=case.revision,
            refund_revision=None,
            event_type=event_type,
            actor_type=CompensationEventActorType.SYSTEM,
            actor_id=None,
            reason_code=reason_code,
            event_metadata=metadata,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _require_owner(actual: str, expected: str) -> None:
        if not compare_digest(actual, expected):
            raise CompensationForbiddenError(
                "The authenticated account does not own this compensation evidence",
                "COMPENSATION_OWNERSHIP_MISMATCH",
            )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Compensation timestamps must be timezone-aware")
        return value.astimezone(UTC)


class RefundApplicationService:
    """Reserve once, perform provider I/O unlocked, and converge monotonically."""

    def __init__(
        self,
        session: AsyncSession,
        provider: RefundProvider | None,
        settings: Settings,
        *,
        clock=utc_now,
    ) -> None:
        self._session = session
        self._provider = provider
        self._settings = settings
        self._clock = clock
        self._cases = CompensationCaseRepository(session)
        self._events = CompensationEventRepository(session)
        self._transactions = PaymentTransactionRepository(session)
        self._attempts = PaymentAttemptRepository(session)
        self._refunds = PaymentRefundRepository(session)

    async def execute_case(self, compensation_case_id: str) -> RefundExecutionResult:
        """Execute or resume one case without holding locks over provider I/O."""
        self._require_enabled()
        provider = self._require_provider()
        refund = await self._prepare_refund(compensation_case_id)
        initial_state = effective_refund_state(refund)
        if initial_state in {PaymentRefundState.REFUNDED, PaymentRefundState.REFUND_FAILED}:
            return RefundExecutionResult(
                refund.id,
                initial_state,
                True,
                refund_reason_code(refund),
            )

        # This preflight is side-effect free. Only a never-attempted pending
        # reservation may proceed to creation when no exact receipt exists.
        try:
            evidence = await self._find_existing_provider_refund(provider, refund)
        except PaymentProviderTimeoutError as error:
            if initial_state is PaymentRefundState.REFUND_PENDING:
                raise CompensationTimeoutError(
                    "Razorpay refund preflight timed out before creation",
                    "REFUND_PROVIDER_UNAVAILABLE",
                ) from error
            if initial_state is PaymentRefundState.RECONCILIATION_REQUIRED:
                raise CompensationUnavailableError(
                    "Razorpay refund still requires reconciliation",
                    "REFUND_RECONCILIATION_REQUIRED",
                ) from error
            if initial_state is PaymentRefundState.REFUND_PROCESSING:
                await self._mark_uncertain(refund.id, reason_code="REFUND_PROVIDER_TIMEOUT")
            raise CompensationTimeoutError(
                "Razorpay refund recovery timed out ambiguously",
                "REFUND_CREATION_UNCERTAIN",
            ) from error
        except PaymentProviderUnavailableError as error:
            if initial_state is PaymentRefundState.REFUND_PENDING:
                raise CompensationUnavailableError(
                    "Razorpay refund preflight is temporarily unavailable",
                    "REFUND_PROVIDER_UNAVAILABLE",
                ) from error
            if initial_state is PaymentRefundState.RECONCILIATION_REQUIRED:
                raise CompensationUnavailableError(
                    "Razorpay refund still requires reconciliation",
                    "REFUND_RECONCILIATION_REQUIRED",
                ) from error
            if initial_state is PaymentRefundState.REFUND_PROCESSING:
                await self._mark_uncertain(refund.id, reason_code="REFUND_PROVIDER_UNAVAILABLE")
            raise CompensationUnavailableError(
                "Razorpay refund recovery is temporarily unavailable",
                "REFUND_CREATION_UNCERTAIN",
            ) from error
        except PaymentProviderRejectedError as error:
            await self._mark_reconciliation_required(
                refund.id, reason_code="REFUND_PROVIDER_LOOKUP_FAILED"
            )
            raise CompensationUnavailableError(
                "Razorpay could not locate the reserved refund safely",
                "REFUND_RECONCILIATION_REQUIRED",
            ) from error
        except PaymentProviderResponseError as error:
            await self._mark_reconciliation_required(
                refund.id, reason_code="REFUND_PROVIDER_RESPONSE_MISMATCH"
            )
            raise CompensationIntegrityError(
                "Razorpay returned conflicting refund recovery evidence",
                "REFUND_PROVIDER_RESPONSE_MISMATCH",
            ) from error

        if evidence is None and initial_state is not PaymentRefundState.REFUND_PENDING:
            if initial_state is PaymentRefundState.REFUND_PROCESSING:
                await self._mark_uncertain(
                    refund.id,
                    reason_code="REFUND_PROVIDER_REFUND_NOT_FOUND",
                )
            reason_code = (
                "REFUND_RECONCILIATION_REQUIRED"
                if initial_state is PaymentRefundState.RECONCILIATION_REQUIRED
                else "REFUND_CREATION_UNCERTAIN"
            )
            raise CompensationUnavailableError(
                "No exact provider refund was found after a refund attempt began",
                reason_code,
            )

        if initial_state is PaymentRefundState.REFUND_PENDING:
            refund, owns_create_attempt = await self._mark_request_started(refund.id)
            if evidence is None and not owns_create_attempt:
                current_state = effective_refund_state(refund)
                return RefundExecutionResult(
                    refund.id,
                    current_state,
                    current_state
                    in {PaymentRefundState.REFUNDED, PaymentRefundState.REFUND_FAILED},
                    refund_reason_code(refund),
                )

        if evidence is None:
            dispatch = await self._begin_dispatch(refund.id)
            request = CreateProviderRefund(
                payment_id=refund.provider_payment_id,
                amount=refund.amount,
                currency=refund.currency,
                receipt=refund.provider_receipt,
                notes={
                    "metergate_refund_id": refund.id,
                    "compensation_case_id": refund.compensation_case_id,
                    "transaction_id": refund.transaction_id,
                },
            )
            try:
                evidence = await provider.create_refund(request)
            except PaymentProviderRejectedError:
                failed = await self._mark_failed(
                    refund.id,
                    reason_code="REFUND_CREATION_FAILED",
                    actor_type=CompensationEventActorType.PROVIDER_API,
                )
                return RefundExecutionResult(
                    failed.id,
                    PaymentRefundState(failed.refund_state),
                    True,
                    "REFUND_CREATION_FAILED",
                )
            except PaymentProviderTimeoutError as error:
                await self._mark_uncertain(refund.id, reason_code="REFUND_CREATION_UNCERTAIN")
                raise CompensationTimeoutError(
                    "Razorpay refund creation timed out ambiguously",
                    "REFUND_CREATION_UNCERTAIN",
                ) from error
            except PaymentProviderUnavailableError as error:
                await self._mark_uncertain(refund.id, reason_code="REFUND_PROVIDER_UNAVAILABLE")
                raise CompensationUnavailableError(
                    "Razorpay refund creation is temporarily unavailable",
                    "REFUND_PROVIDER_UNAVAILABLE",
                ) from error
            except PaymentProviderResponseError as error:
                await self._mark_reconciliation_required(
                    refund.id, reason_code="REFUND_PROVIDER_RESPONSE_MISMATCH"
                )
                raise CompensationIntegrityError(
                    "Razorpay returned mismatched refund creation evidence",
                    "REFUND_PROVIDER_RESPONSE_MISMATCH",
                ) from error
            await self._finish_dispatch(dispatch.id, evidence.id, "provider_response_received")

        applied = await self.apply_provider_evidence(
            refund.id,
            evidence,
            actor_type=CompensationEventActorType.PROVIDER_API,
            actor_id=evidence.id,
            idempotency_key=(
                f"provider:{evidence.id}:{evidence.status}:{evidence.amount}:{evidence.payment_id}"
            ),
            observed_at=self._read_clock(),
        )
        final_state = effective_refund_state(applied)
        terminal = final_state in {
            PaymentRefundState.REFUNDED,
            PaymentRefundState.REFUND_FAILED,
        }
        return RefundExecutionResult(
            applied.id,
            final_state,
            terminal,
            refund_reason_code(applied),
        )

    async def reconcile_refund(
        self,
        refund_id: str,
        *,
        account_id: str | None = None,
    ) -> PaymentRefund:
        """Fetch authoritative provider evidence and converge one local refund."""
        self._require_enabled()
        provider = self._require_provider()
        refund = await self._refunds.get(refund_id)
        if refund is None:
            raise CompensationNotFoundError("Refund was not found", "REFUND_NOT_FOUND")
        case = await self._cases.get(refund.compensation_case_id)
        if case is None:
            raise CompensationIntegrityError(
                "Refund compensation evidence is missing", "REFUND_CASE_EVIDENCE_MISSING"
            )
        if account_id is not None:
            CompensationApplicationService._require_owner(case.account_id, account_id)
        await self._session.commit()
        try:
            evidence = await self._find_existing_provider_refund(provider, refund)
        except PaymentProviderTimeoutError as error:
            raise CompensationTimeoutError(
                "Razorpay refund reconciliation timed out",
                "REFUND_PROVIDER_UNAVAILABLE",
            ) from error
        except PaymentProviderUnavailableError as error:
            raise CompensationUnavailableError(
                "Razorpay refund reconciliation is unavailable",
                "REFUND_PROVIDER_UNAVAILABLE",
            ) from error
        except (PaymentProviderRejectedError, PaymentProviderResponseError) as error:
            await self._mark_reconciliation_required(
                refund.id,
                reason_code="REFUND_PROVIDER_RESPONSE_MISMATCH",
                actor_type=CompensationEventActorType.PROVIDER_API,
            )
            raise CompensationIntegrityError(
                "Razorpay refund evidence could not be reconciled",
                "REFUND_RECONCILIATION_REQUIRED",
            ) from error
        if evidence is None:
            # A reservation that has never crossed the provider boundary has
            # nothing to reconcile.  In particular, a buyer refresh must not
            # be able to move queued work out of refund_pending before the
            # durable worker owns the create attempt.
            if (
                PaymentRefundState(refund.refund_state) is PaymentRefundState.REFUND_PENDING
                and refund.requested_at is None
            ):
                return refund
            return await self._mark_reconciliation_required(
                refund.id,
                reason_code="REFUND_PROVIDER_REFUND_NOT_FOUND",
                actor_type=CompensationEventActorType.PROVIDER_API,
            )
        return await self.apply_provider_evidence(
            refund.id,
            evidence,
            actor_type=CompensationEventActorType.PROVIDER_API,
            actor_id=evidence.id,
            idempotency_key=(
                f"reconcile:{evidence.id}:{evidence.status}:{evidence.amount}:{evidence.payment_id}"
            ),
            observed_at=self._read_clock(),
            reconciled=True,
        )

    async def process_refund_webhook(self, payload: VerifiedWebhookPayload) -> None:
        """Apply one already-authenticated raw webhook with event-ID idempotency."""
        try:
            evidence = parse_refund_webhook(payload.raw_body)
        except (TypeError, ValueError) as error:
            raise CompensationIntegrityError(
                "Signed Razorpay refund webhook evidence is invalid",
                "REFUND_WEBHOOK_PAYLOAD_INVALID",
            ) from error
        idempotency_key = f"webhook:{payload.provider_event_id}"
        existing_event = await self._events.get_by_idempotency_key(idempotency_key)
        if existing_event is not None:
            return
        refund = await self._refunds.get_by_provider_refund_id(evidence.refund.id)
        if refund is None and evidence.refund.receipt is not None:
            refund = await self._refunds.get_by_provider_receipt(evidence.refund.receipt)
        if refund is None:
            raise CompensationNotFoundError(
                "Signed refund webhook lacks an exact reserved refund identity",
                "REFUND_WEBHOOK_UNMATCHED",
            )
        transaction = await self._transactions.get_for_update(refund.transaction_id)
        if (
            transaction is None
            or transaction.provider_order_id is None
            or transaction.provider_order_id != evidence.provider_order_id
            or transaction.amount != evidence.payment_amount
        ):
            await self._session.rollback()
            await self._mark_reconciliation_required(
                refund.id,
                reason_code="REFUND_PROVIDER_RESPONSE_MISMATCH",
                actor_type=CompensationEventActorType.PROVIDER_WEBHOOK,
                actor_id=payload.provider_event_id,
                idempotency_key=idempotency_key,
                metadata={"provider_order_id": evidence.provider_order_id},
            )
            raise CompensationIntegrityError(
                "Signed refund webhook does not match the reserved Razorpay payment",
                "REFUND_PROVIDER_RESPONSE_MISMATCH",
            )
        local_refunds = await self._refunds.list_for_transaction(refund.transaction_id)
        locally_refunded = sum(
            local.amount
            for local in local_refunds
            if PaymentRefundState(local.refund_state) is PaymentRefundState.REFUNDED
        )
        expected_cumulative = locally_refunded
        if (
            evidence.refund.status == "processed"
            and PaymentRefundState(refund.refund_state) is not PaymentRefundState.REFUNDED
        ):
            expected_cumulative += refund.amount
        if (
            evidence.refund.status == "processed"
            and evidence.payment_amount_refunded != expected_cumulative
        ):
            await self._session.rollback()
            await self._mark_reconciliation_required(
                refund.id,
                reason_code="REFUND_PROVIDER_RESPONSE_MISMATCH",
                actor_type=CompensationEventActorType.PROVIDER_WEBHOOK,
                actor_id=payload.provider_event_id,
                idempotency_key=idempotency_key,
                metadata={
                    "provider_refund_id": evidence.refund.id,
                    "provider_amount_refunded": evidence.payment_amount_refunded,
                    "locally_refunded": locally_refunded,
                },
            )
            raise CompensationIntegrityError(
                "Signed refund webhook cumulative amount conflicts with local refund evidence",
                "REFUND_PROVIDER_RESPONSE_MISMATCH",
            )
        await self.apply_provider_evidence(
            refund.id,
            evidence.refund,
            actor_type=CompensationEventActorType.PROVIDER_WEBHOOK,
            actor_id=payload.provider_event_id,
            idempotency_key=idempotency_key,
            observed_at=max(
                self._as_utc(payload.received_at),
                self._as_utc(evidence.provider_created_at),
            ),
        )

    async def apply_provider_evidence(
        self,
        refund_id: str,
        evidence: ProviderRefund,
        *,
        actor_type: CompensationEventActorType,
        actor_id: str,
        idempotency_key: str,
        observed_at: datetime,
        reconciled: bool = False,
    ) -> PaymentRefund:
        """Validate and apply provider status without regressing terminal state."""
        if await self._events.get_by_idempotency_key(idempotency_key) is not None:
            existing = await self._refunds.get(refund_id)
            if existing is None:
                raise CompensationIntegrityError(
                    "Refund audit evidence lost its aggregate", "REFUND_EVIDENCE_MISSING"
                )
            if not (reconciled and existing.reconciliation_required_at is not None):
                return existing
        refund = await self._refunds.get_for_update(refund_id)
        if refund is None:
            raise CompensationNotFoundError("Refund was not found", "REFUND_NOT_FOUND")
        case = await self._cases.get_for_update(refund.compensation_case_id)
        if case is None:
            raise CompensationIntegrityError(
                "Refund compensation evidence is missing", "REFUND_CASE_EVIDENCE_MISSING"
            )
        mismatch = (
            evidence.payment_id != refund.provider_payment_id
            or evidence.amount != refund.amount
            or (evidence.currency is not None and evidence.currency != refund.currency)
            or (evidence.receipt is not None and evidence.receipt != refund.provider_receipt)
            or (refund.provider_refund_id is not None and evidence.id != refund.provider_refund_id)
        )
        if mismatch:
            await self._session.rollback()
            await self._mark_reconciliation_required(
                refund_id,
                reason_code="REFUND_PROVIDER_RESPONSE_MISMATCH",
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                metadata={
                    "provider_refund_id": evidence.id,
                    "provider_status": evidence.status,
                },
            )
            raise CompensationIntegrityError(
                "Provider refund evidence does not match the reserved refund",
                "REFUND_PROVIDER_RESPONSE_MISMATCH",
            )
        current = PaymentRefundState(refund.refund_state)
        if current is PaymentRefundState.REFUNDED:
            if evidence.status == "failed":
                await self._session.rollback()
                await self._mark_reconciliation_required(
                    refund_id,
                    reason_code="REFUND_TERMINAL_EVIDENCE_CONFLICT",
                    actor_type=actor_type,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    metadata={
                        "stored_terminal_state": current.value,
                        "incoming_provider_status": evidence.status,
                        "provider_refund_id": evidence.id,
                    },
                )
                raise CompensationIntegrityError(
                    "Terminal refund evidence conflicts with a later provider status",
                    "REFUND_RECONCILIATION_REQUIRED",
                )
            if evidence.status == "processed" and reconciled:
                return await self._resolve_terminal_reconciliation(
                    refund,
                    case,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    observed_at=observed_at,
                    evidence=evidence,
                )
            return refund
        if current is PaymentRefundState.REFUND_FAILED:
            if evidence.status == "processed":
                await self._session.rollback()
                await self._mark_reconciliation_required(
                    refund_id,
                    reason_code="REFUND_TERMINAL_EVIDENCE_CONFLICT",
                    actor_type=actor_type,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    metadata={
                        "stored_terminal_state": current.value,
                        "incoming_provider_status": evidence.status,
                        "provider_refund_id": evidence.id,
                    },
                )
                raise CompensationIntegrityError(
                    "Terminal failed refund conflicts with a later provider status",
                    "REFUND_RECONCILIATION_REQUIRED",
                )
            if evidence.status == "failed" and reconciled:
                return await self._resolve_terminal_reconciliation(
                    refund,
                    case,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    observed_at=observed_at,
                    evidence=evidence,
                )
            return refund

        now = self._as_utc(observed_at)
        was_unbound = refund.provider_refund_id is None
        refunded_total: int | None = None
        if evidence.status == "processed":
            refunded_before = await self._session.scalar(
                select(func.coalesce(func.sum(PaymentRefund.amount), 0)).where(
                    PaymentRefund.compensation_case_id == case.id,
                    PaymentRefund.refund_state == PaymentRefundState.REFUNDED,
                    PaymentRefund.id != refund.id,
                )
            )
            refunded_total = int(refunded_before or 0) + refund.amount
            if case.approved_refund_amount is None or refunded_total > case.approved_refund_amount:
                await self._session.rollback()
                await self._mark_reconciliation_required(
                    refund_id,
                    reason_code="REFUND_AMOUNT_EXCEEDS_APPROVED",
                    actor_type=actor_type,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    metadata={"provider_refund_id": evidence.id},
                )
                raise CompensationIntegrityError(
                    "Processed refunds exceed the approved compensation amount",
                    "REFUND_RECONCILIATION_REQUIRED",
                )
        if refund.requested_at is None:
            refund.requested_at = now
        refund.provider_refund_id = evidence.id
        refund.provider_status = evidence.status
        if reconciled:
            refund.last_reconciled_at = now
        event_type: CompensationEventType
        reason_code: str
        close_case = False
        if evidence.status == "processed":
            refund.refund_state = PaymentRefundState.REFUNDED
            refund.processed_at = now
            event_type = CompensationEventType.REFUND_COMPLETED
            reason_code = "REFUND_COMPLETED"
            close_case = refunded_total == case.approved_refund_amount
        elif evidence.status == "failed":
            refund.refund_state = PaymentRefundState.REFUND_FAILED
            refund.failed_at = now
            event_type = CompensationEventType.REFUND_FAILED
            reason_code = "REFUND_FAILED"
            if CompensationDecisionState(case.decision_state) is not (
                CompensationDecisionState.EXECUTING
            ):
                raise CompensationIntegrityError(
                    "Failed refund is not bound to an executing compensation case",
                    "REFUND_CASE_STATE_CONFLICT",
                )
            case.decision_state = CompensationDecisionState.MANUAL_REVIEW
            case.revision += 1
        else:
            refund.refund_state = PaymentRefundState.REFUND_PROCESSING
            event_type = CompensationEventType.REFUND_PROCESSING
            reason_code = "REFUND_PROCESSING"
        refund.revision += 1
        if close_case:
            if CompensationDecisionState(case.decision_state) is not (
                CompensationDecisionState.EXECUTING
            ):
                raise CompensationIntegrityError(
                    "Processed refund is not bound to an executing compensation case",
                    "REFUND_CASE_STATE_CONFLICT",
                )
            case.decision_state = CompensationDecisionState.COMPLETED
            # The provider observation clock may be injected and can precede
            # a database server timestamp by a small amount. Closure must
            # never predate the durable decision evidence.
            case.closed_at = max(now, self._as_utc(case.decided_at))
            case.revision += 1
        sequence = await self._next_sequence(case.id)
        events: list[CompensationEvent] = []
        if was_unbound:
            events.append(
                self._refund_event(
                    case,
                    refund,
                    sequence=sequence,
                    event_type=CompensationEventType.RAZORPAY_REFUND_CREATED,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    reason_code="RAZORPAY_REFUND_CREATED",
                    idempotency_key=f"{idempotency_key}:created",
                    occurred_at=now,
                    metadata={"provider_refund_id": evidence.id},
                )
            )
            sequence += 1
        events.append(
            self._refund_event(
                case,
                refund,
                sequence=sequence,
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                reason_code=reason_code,
                idempotency_key=idempotency_key,
                occurred_at=now,
                metadata={
                    "provider_refund_id": evidence.id,
                    "provider_status": evidence.status,
                    "amount": evidence.amount,
                    "currency": refund.currency,
                },
            )
        )
        if close_case:
            events.append(
                self._refund_event(
                    case,
                    refund,
                    sequence=sequence + 1,
                    event_type=CompensationEventType.COMPENSATION_CLOSED,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    reason_code="COMPENSATION_CLOSED_REFUNDED",
                    idempotency_key=f"{idempotency_key}:case-closed",
                    occurred_at=now,
                    metadata={"provider_refund_id": evidence.id},
                )
            )
        self._session.add_all(events)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            duplicate = await self._events.get_by_idempotency_key(idempotency_key)
            if duplicate is None:
                raise
        current_refund = await self._refunds.get(refund_id)
        if current_refund is None:
            raise CompensationIntegrityError(
                "Refund disappeared after provider evidence commit",
                "REFUND_EVIDENCE_MISSING",
            )
        return current_refund

    async def _resolve_terminal_reconciliation(
        self,
        refund: PaymentRefund,
        case: CompensationCase,
        *,
        actor_type: CompensationEventActorType,
        actor_id: str,
        idempotency_key: str,
        observed_at: datetime,
        evidence: ProviderRefund,
    ) -> PaymentRefund:
        """Clear only the orthogonal anomaly overlay after an exact API read."""
        if refund.reconciliation_required_at is None:
            return refund
        now = self._as_utc(observed_at)
        if refund.last_reconciled_at is not None:
            now = max(now, self._as_utc(refund.last_reconciled_at))
        prior_reason = refund.reconciliation_reason_code
        resolution_key = f"{idempotency_key}:resolved:{refund.revision + 1}"
        refund.reconciliation_required_at = None
        refund.reconciliation_reason_code = None
        refund.last_reconciled_at = now
        refund.revision += 1
        sequence = await self._next_sequence(case.id)
        self._session.add(
            self._refund_event(
                case,
                refund,
                sequence=sequence,
                event_type=CompensationEventType.REFUND_RECONCILIATION_RESOLVED,
                actor_type=actor_type,
                actor_id=actor_id,
                reason_code="REFUND_RECONCILIATION_RESOLVED",
                idempotency_key=resolution_key,
                occurred_at=now,
                metadata={
                    "prior_reason_code": prior_reason,
                    "provider_refund_id": evidence.id,
                    "provider_status": evidence.status,
                },
            )
        )
        await self._session.commit()
        await self._session.refresh(refund)
        return refund

    async def _prepare_refund(self, case_id: str) -> PaymentRefund:
        preliminary = await self._cases.get(case_id)
        if preliminary is None:
            raise CompensationNotFoundError(
                "Compensation case was not found", "COMPENSATION_NOT_FOUND"
            )
        transaction = await self._transactions.get_for_update(preliminary.transaction_id)
        if transaction is None:
            raise CompensationIntegrityError(
                "Compensation payment transaction is missing",
                "COMPENSATION_PAYMENT_MISSING",
            )
        case = await self._cases.get_for_update(case_id)
        if case is None:
            raise CompensationNotFoundError(
                "Compensation case was not found", "COMPENSATION_NOT_FOUND"
            )
        existing = await self._refunds.get_by_compensation_case_id(case.id)
        if existing is not None:
            await self._session.commit()
            return existing
        if CompensationDecisionState(case.decision_state) is not (
            CompensationDecisionState.APPROVED
        ):
            raise CompensationConflictError(
                "Compensation case is not approved for a refund",
                "COMPENSATION_NOT_APPROVED",
            )
        amount = case.approved_refund_amount
        if amount is None or not 0 < amount <= case.amount_paid:
            raise CompensationIntegrityError(
                "Approved refund amount is invalid", "REFUND_AMOUNT_INVALID"
            )
        if (
            PaymentTransactionState(transaction.transaction_state)
            is not PaymentTransactionState.PAID
            or transaction.id != case.transaction_id
            or transaction.amount != case.amount_paid
            or transaction.currency != case.currency
        ):
            raise CompensationIntegrityError(
                "Compensation no longer matches paid transaction evidence",
                "COMPENSATION_PAYMENT_MISMATCH",
            )
        attempt = await self._attempts.get(case.payment_attempt_id)
        if (
            attempt is None
            or attempt.transaction_id != transaction.id
            or not attempt.captured
            or PaymentAttemptStatus(attempt.provider_status)
            not in {PaymentAttemptStatus.CAPTURED, PaymentAttemptStatus.REFUNDED}
            or attempt.amount != transaction.amount
            or attempt.currency != transaction.currency
        ):
            raise CompensationIntegrityError(
                "Captured payment evidence does not authorize this refund",
                "COMPENSATION_CAPTURE_EVIDENCE_CONFLICT",
            )
        reserved = await self._session.scalar(
            select(func.coalesce(func.sum(PaymentRefund.amount), 0)).where(
                PaymentRefund.payment_attempt_id == attempt.id,
                PaymentRefund.refund_state != PaymentRefundState.REFUND_FAILED,
            )
        )
        if int(reserved or 0) + amount > attempt.amount:
            raise CompensationConflictError(
                "Refund would exceed the captured payment amount",
                "REFUND_AMOUNT_EXCEEDS_CAPTURED",
            )
        now = self._read_clock()
        refund_id = new_payment_refund_id()
        refund = PaymentRefund(
            id=refund_id,
            compensation_case_id=case.id,
            transaction_id=transaction.id,
            payment_attempt_id=attempt.id,
            provider=PaymentProvider.RAZORPAY,
            provider_payment_id=attempt.provider_payment_id,
            provider_refund_id=None,
            amount=amount,
            currency=transaction.currency,
            refund_state=PaymentRefundState.REFUND_PENDING,
            provider_status=None,
            provider_receipt=refund_id,
            created_at=now,
            requested_at=None,
            processed_at=None,
            failed_at=None,
            last_reconciled_at=None,
            revision=1,
        )
        case.decision_state = CompensationDecisionState.EXECUTING
        case.revision += 1
        sequence = await self._next_sequence(case.id)
        self._refunds.stage(refund)
        # The audit row carries only scalar foreign-key identifiers, so the ORM
        # has no relationship edge with which to order these two inserts.
        # Materialize the refund before staging its immediately constrained FK.
        await self._session.flush()
        self._session.add(
            self._refund_event(
                case,
                refund,
                sequence=sequence,
                event_type=CompensationEventType.REFUND_REQUESTED,
                actor_type=CompensationEventActorType.REFUND_WORKER,
                actor_id=None,
                reason_code="REFUND_REQUESTED",
                idempotency_key=f"refund-requested:{case.id}",
                occurred_at=now,
                metadata={"amount": amount, "currency": refund.currency},
            )
        )
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            existing = await self._refunds.get_by_compensation_case_id(case.id)
            if existing is None:
                raise
            return existing
        await self._session.refresh(refund)
        return refund

    async def _mark_request_started(self, refund_id: str) -> tuple[PaymentRefund, bool]:
        refund = await self._refunds.get_for_update(refund_id)
        if refund is None:
            raise CompensationNotFoundError("Refund was not found", "REFUND_NOT_FOUND")
        state = PaymentRefundState(refund.refund_state)
        if state is not PaymentRefundState.REFUND_PENDING:
            await self._session.commit()
            return refund, False
        case = await self._cases.get_for_update(refund.compensation_case_id)
        if case is None or CompensationDecisionState(case.decision_state) is not (
            CompensationDecisionState.EXECUTING
        ):
            raise CompensationIntegrityError(
                "Refund request is not bound to an executing compensation case",
                "REFUND_CASE_STATE_CONFLICT",
            )
        now = self._read_clock()
        refund.refund_state = PaymentRefundState.REFUND_PROCESSING
        if refund.requested_at is None:
            refund.requested_at = now
        refund.revision += 1
        sequence = await self._next_sequence(case.id)
        self._session.add(
            self._refund_event(
                case,
                refund,
                sequence=sequence,
                event_type=CompensationEventType.REFUND_REQUEST_STARTED,
                actor_type=CompensationEventActorType.REFUND_WORKER,
                actor_id=None,
                reason_code="REFUND_REQUEST_STARTED",
                idempotency_key=f"refund-started:{refund.id}:{refund.revision}",
                occurred_at=now,
                metadata={"provider_receipt": refund.provider_receipt},
            )
        )
        await self._session.commit()
        await self._session.refresh(refund)
        return refund, True

    async def _begin_dispatch(self, refund_id: str) -> RefundDispatchAttempt:
        """Commit one stable intent before crossing the provider boundary."""
        existing = await self._session.scalar(
            select(RefundDispatchAttempt).where(RefundDispatchAttempt.refund_id == refund_id)
        )
        now = self._read_clock()
        if existing is None:
            existing = RefundDispatchAttempt(
                refund_id=refund_id,
                dispatch_generation=1,
                provider_idempotency_key=refund_id,
                state="provider_call_started",
                intent_committed_at=now,
                provider_call_started_at=now,
            )
            self._session.add(existing)
        elif existing.provider_idempotency_key != refund_id:
            raise CompensationIntegrityError(
                "Refund dispatch identity conflicts with its reservation",
                "REFUND_DISPATCH_IDENTITY_MISMATCH",
            )
        elif existing.provider_call_started_at is None:
            existing.state = "provider_call_started"
            existing.provider_call_started_at = now
        await self._session.commit()
        await self._session.refresh(existing)
        return existing

    async def _finish_dispatch(
        self, dispatch_id: str, provider_refund_id: str, outcome: str
    ) -> None:
        dispatch = await self._session.get(RefundDispatchAttempt, dispatch_id)
        if dispatch is None:
            raise CompensationIntegrityError(
                "Refund dispatch evidence disappeared",
                "REFUND_DISPATCH_EVIDENCE_MISSING",
            )
        dispatch.state = "provider_call_finished"
        dispatch.provider_call_finished_at = self._read_clock()
        dispatch.provider_refund_id = provider_refund_id
        dispatch.outcome = outcome
        await self._session.commit()

    async def _find_existing_provider_refund(
        self, provider: RefundProvider, refund: PaymentRefund
    ) -> ProviderRefund | None:
        candidates = await provider.fetch_refunds_for_payment(refund.provider_payment_id)
        attempt = await self._attempts.get(refund.payment_attempt_id)
        if (
            attempt is None
            or attempt.provider_payment_id != refund.provider_payment_id
            or not attempt.captured
        ):
            raise PaymentProviderResponseError(
                "fetch_refunds_for_payment",
                "Captured payment evidence is unavailable for refund recovery",
            )
        local_refunds = await self._refunds.list_for_transaction(refund.transaction_id)
        provider_reserved_total = sum(
            candidate.amount for candidate in candidates if candidate.status != "failed"
        )
        if provider_reserved_total > attempt.amount:
            raise PaymentProviderResponseError(
                "fetch_refunds_for_payment",
                "Razorpay refund total exceeds the captured payment amount",
            )
        matched_local_ids: set[str] = set()
        target_matches: list[ProviderRefund] = []
        for candidate in candidates:
            local_matches = [
                local
                for local in local_refunds
                if (
                    local.provider_refund_id is not None
                    and local.provider_refund_id == candidate.id
                )
                or (candidate.receipt is not None and local.provider_receipt == candidate.receipt)
            ]
            if len(local_matches) != 1:
                raise PaymentProviderResponseError(
                    "fetch_refunds_for_payment",
                    "Razorpay returned a refund without one exact MeterGate reservation",
                )
            local = local_matches[0]
            if local.id in matched_local_ids:
                raise PaymentProviderResponseError(
                    "fetch_refunds_for_payment",
                    "Razorpay returned multiple refunds for one MeterGate reservation",
                )
            matched_local_ids.add(local.id)
            if (
                candidate.payment_id != local.provider_payment_id
                or candidate.amount != local.amount
                or (candidate.currency is not None and candidate.currency != local.currency)
                or (
                    local.provider_refund_id is not None
                    and candidate.id != local.provider_refund_id
                )
            ):
                raise PaymentProviderResponseError(
                    "fetch_refunds_for_payment",
                    "Razorpay refund terms conflict with MeterGate evidence",
                )
            if local.id == refund.id:
                target_matches.append(candidate)
        if len(target_matches) > 1:
            raise PaymentProviderResponseError(
                "fetch_refunds_for_payment",
                "Razorpay returned multiple refunds for one MeterGate receipt",
            )
        return target_matches[0] if target_matches else None

    async def _mark_uncertain(self, refund_id: str, *, reason_code: str) -> PaymentRefund:
        return await self._mark_nonterminal(
            refund_id,
            state=PaymentRefundState.REFUND_UNCERTAIN,
            event_type=CompensationEventType.REFUND_UNCERTAIN,
            reason_code=reason_code,
        )

    async def _mark_reconciliation_required(
        self,
        refund_id: str,
        *,
        reason_code: str,
        actor_type: CompensationEventActorType = CompensationEventActorType.REFUND_WORKER,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PaymentRefund:
        return await self._mark_nonterminal(
            refund_id,
            state=PaymentRefundState.RECONCILIATION_REQUIRED,
            event_type=CompensationEventType.REFUND_RECONCILIATION_REQUIRED,
            reason_code=reason_code,
            reconciled=True,
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    async def _mark_nonterminal(
        self,
        refund_id: str,
        *,
        state: PaymentRefundState,
        event_type: CompensationEventType,
        reason_code: str,
        reconciled: bool = False,
        actor_type: CompensationEventActorType = CompensationEventActorType.REFUND_WORKER,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PaymentRefund:
        refund = await self._refunds.get_for_update(refund_id)
        if refund is None:
            raise CompensationNotFoundError("Refund was not found", "REFUND_NOT_FOUND")
        current = PaymentRefundState(refund.refund_state)
        case = await self._cases.get_for_update(refund.compensation_case_id)
        if case is None:
            raise CompensationIntegrityError(
                "Refund compensation evidence is missing", "REFUND_CASE_EVIDENCE_MISSING"
            )
        now = self._read_clock()
        terminal = current in {
            PaymentRefundState.REFUNDED,
            PaymentRefundState.REFUND_FAILED,
        }
        if terminal:
            if state is not PaymentRefundState.RECONCILIATION_REQUIRED:
                await self._session.commit()
                return refund
            refund.reconciliation_required_at = max(
                now,
                self._as_utc(refund.reconciliation_required_at)
                if refund.reconciliation_required_at is not None
                else now,
            )
            refund.reconciliation_reason_code = reason_code
        else:
            if refund.requested_at is None:
                refund.requested_at = now
            if current is not state:
                refund.refund_state = state
        if reconciled:
            refund.last_reconciled_at = max(
                now,
                self._as_utc(refund.last_reconciled_at)
                if refund.last_reconciled_at is not None
                else now,
            )
        refund.revision += 1
        sequence = await self._next_sequence(case.id)
        self._session.add(
            self._refund_event(
                case,
                refund,
                sequence=sequence,
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                reason_code=reason_code,
                idempotency_key=(
                    idempotency_key or f"{event_type.value}:{refund.id}:{refund.revision}"
                ),
                occurred_at=now,
                metadata=metadata or {},
            )
        )
        await self._session.commit()
        await self._session.refresh(refund)
        return refund

    async def _mark_failed(
        self,
        refund_id: str,
        *,
        reason_code: str,
        actor_type: CompensationEventActorType,
    ) -> PaymentRefund:
        refund = await self._refunds.get_for_update(refund_id)
        if refund is None:
            raise CompensationNotFoundError("Refund was not found", "REFUND_NOT_FOUND")
        current = PaymentRefundState(refund.refund_state)
        if current in {PaymentRefundState.REFUNDED, PaymentRefundState.REFUND_FAILED}:
            await self._session.commit()
            return refund
        case = await self._cases.get_for_update(refund.compensation_case_id)
        if case is None or CompensationDecisionState(case.decision_state) is not (
            CompensationDecisionState.EXECUTING
        ):
            raise CompensationIntegrityError(
                "Failed refund is not bound to an executing compensation case",
                "REFUND_CASE_STATE_CONFLICT",
            )
        now = self._read_clock()
        if refund.requested_at is None:
            refund.requested_at = now
        refund.refund_state = PaymentRefundState.REFUND_FAILED
        # A definite API rejection means no provider refund entity was created;
        # do not fabricate a provider-side failed status or identifier.
        refund.provider_status = None
        refund.failed_at = now
        refund.revision += 1
        case.decision_state = CompensationDecisionState.MANUAL_REVIEW
        case.revision += 1
        sequence = await self._next_sequence(case.id)
        self._session.add(
            self._refund_event(
                case,
                refund,
                sequence=sequence,
                event_type=CompensationEventType.REFUND_FAILED,
                actor_type=actor_type,
                actor_id=None,
                reason_code=reason_code,
                idempotency_key=f"refund-failed:{refund.id}:{refund.revision}",
                occurred_at=now,
                metadata={},
            )
        )
        await self._session.commit()
        await self._session.refresh(refund)
        return refund

    async def _next_sequence(self, case_id: str) -> int:
        value = await self._session.scalar(
            select(func.coalesce(func.max(CompensationEvent.sequence), 0)).where(
                CompensationEvent.compensation_case_id == case_id
            )
        )
        return int(value or 0) + 1

    @staticmethod
    def _refund_event(
        case: CompensationCase,
        refund: PaymentRefund,
        *,
        sequence: int,
        event_type: CompensationEventType,
        actor_type: CompensationEventActorType,
        actor_id: str | None,
        reason_code: str,
        idempotency_key: str,
        occurred_at: datetime,
        metadata: dict[str, object],
    ) -> CompensationEvent:
        return CompensationEvent(
            id=new_compensation_event_id(),
            compensation_case_id=case.id,
            transaction_id=case.transaction_id,
            payment_refund_id=refund.id,
            sequence=sequence,
            case_revision=case.revision,
            refund_revision=refund.revision,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code=reason_code,
            event_metadata=metadata,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )

    def _require_enabled(self) -> None:
        if not self._settings.refunds_enabled:
            raise CompensationUnavailableError(
                "Razorpay Test Mode refunds are disabled", "REFUND_DISABLED"
            )

    def _require_provider(self) -> RefundProvider:
        if self._provider is None:
            raise CompensationUnavailableError(
                "Razorpay Test Mode refund provider is unavailable",
                "REFUND_PROVIDER_UNAVAILABLE",
            )
        return self._provider

    def _read_clock(self) -> datetime:
        return self._as_utc(self._clock())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Refund timestamps must be timezone-aware")
        return value.astimezone(UTC)


def compensation_summary(case: CompensationCase) -> CompensationSummary:
    return CompensationSummary(
        compensation_id=case.id,
        fulfillment_execution_id=case.fulfillment_execution_id,
        failure_code=case.failure_code,
        recommended_action=case.recommended_action.value,
        decision_state=case.decision_state.value,
        decision_provenance=(
            case.decision_provenance.value if case.decision_provenance is not None else None
        ),
        approved_refund_amount=case.approved_refund_amount,
        decision_reason_code=case.decision_reason_code,
    )


def refund_summary(refund: PaymentRefund) -> RefundSummary:
    return RefundSummary(
        refund_id=refund.id,
        provider_payment_id=refund.provider_payment_id,
        provider_refund_id=refund.provider_refund_id,
        amount=refund.amount,
        currency=refund.currency,
        state=effective_refund_state(refund).value,
        provider_status=refund.provider_status,
        reconciliation_required_at=refund.reconciliation_required_at,
        reconciliation_reason_code=refund.reconciliation_reason_code,
    )


def payment_refund_response(refund: PaymentRefund) -> PaymentRefundResponse:
    return PaymentRefundResponse(
        reason_code=refund_reason_code(refund),
        refund_id=refund.id,
        compensation_id=refund.compensation_case_id,
        transaction_id=refund.transaction_id,
        payment_attempt_id=refund.payment_attempt_id,
        provider=refund.provider.value,
        provider_payment_id=refund.provider_payment_id,
        provider_refund_id=refund.provider_refund_id,
        provider_receipt=refund.provider_receipt,
        amount=refund.amount,
        currency=refund.currency,
        state=effective_refund_state(refund).value,
        provider_status=refund.provider_status,
        created_at=refund.created_at,
        requested_at=refund.requested_at,
        processed_at=refund.processed_at,
        failed_at=refund.failed_at,
        last_reconciled_at=refund.last_reconciled_at,
        reconciliation_required_at=refund.reconciliation_required_at,
        reconciliation_reason_code=refund.reconciliation_reason_code,
        revision=refund.revision,
    )


def case_reason_code(case: CompensationCase) -> str:
    state = CompensationDecisionState(case.decision_state)
    return {
        CompensationDecisionState.PENDING: "COMPENSATION_PENDING",
        CompensationDecisionState.APPROVED: "COMPENSATION_APPROVED",
        CompensationDecisionState.REJECTED: "COMPENSATION_REJECTED",
        CompensationDecisionState.EXECUTING: "REFUND_PROCESSING",
        CompensationDecisionState.COMPLETED: "COMPENSATION_COMPLETED",
        CompensationDecisionState.MANUAL_REVIEW: "COMPENSATION_MANUAL_REVIEW_REQUIRED",
    }[state]


def refund_reason_code(refund: PaymentRefund) -> str:
    if refund.reconciliation_required_at is not None:
        return refund.reconciliation_reason_code or "REFUND_RECONCILIATION_REQUIRED"
    state = PaymentRefundState(refund.refund_state)
    return {
        PaymentRefundState.REFUND_PENDING: "REFUND_PENDING",
        PaymentRefundState.REFUND_PROCESSING: "REFUND_PROCESSING",
        PaymentRefundState.REFUNDED: "REFUND_COMPLETED",
        PaymentRefundState.REFUND_FAILED: "REFUND_FAILED",
        PaymentRefundState.REFUND_UNCERTAIN: "REFUND_CREATION_UNCERTAIN",
        PaymentRefundState.RECONCILIATION_REQUIRED: "REFUND_RECONCILIATION_REQUIRED",
    }[state]


def effective_refund_state(refund: PaymentRefund) -> PaymentRefundState:
    """Project an unresolved integrity overlay without rewriting terminal facts."""
    if refund.reconciliation_required_at is not None:
        return PaymentRefundState.RECONCILIATION_REQUIRED
    return PaymentRefundState(refund.refund_state)
