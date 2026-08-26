"""Capability-gated, lease-fenced merchant fulfillment and result replay."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import JSON
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.canonical_json import CanonicalJSONError, canonical_json_bytes
from app.domain.enums import (
    FulfillmentEventActorType,
    FulfillmentEventType,
    FulfillmentExecutionState,
    FulfillmentProviderType,
)
from app.domain.exceptions import (
    CapabilityForbiddenError,
    EntitlementConflictError,
    EntitlementExpiredError,
    EntitlementIntegrityError,
    EntitlementNotFoundError,
    EntitlementUnavailableError,
    FulfillmentConflictError,
    FulfillmentIntegrityError,
    FulfillmentPermanentError,
    FulfillmentRetryableError,
    QuoteInputIssue,
    QuoteInputValidationError,
    ResourceNotFoundError,
    ServiceConfigurationError,
)
from app.domain.hashing import sha256_bytes
from app.domain.ids import new_fulfillment_event_id, new_fulfillment_execution_id
from app.domain.integrity import IntegrityStructureError
from app.domain.json_schema import JSONSchemaConfigurationError, validate_json_instance
from app.domain.media_types import normalize_json_content_type
from app.domain.quote_integrity import verify_quote_integrity
from app.domain.service_input import (
    ServiceInputConfigurationError,
    ServiceInputValueError,
    validate_normalize_and_hash_service_input,
)
from app.models import Entitlement, FulfillmentEvent, FulfillmentExecution
from app.providers.fulfillment import (
    FulfillmentProvider,
    MerchantFulfillmentInProgressError,
    MerchantFulfillmentPermanentError,
    MerchantFulfillmentRequest,
    MerchantFulfillmentResponseError,
    MerchantFulfillmentRetryableError,
)
from app.repositories import (
    CompensationCaseRepository,
    EntitlementRepository,
    FulfillmentExecutionRepository,
    MerchantRepository,
    QuoteRepository,
    ServiceFulfillmentConfigRepository,
    ServiceRepository,
)
from app.services.capabilities import CapabilityTokenService
from app.services.compensations import CompensationApplicationService
from app.services.entitlements import EntitlementApplicationService
from app.services.payments import PaymentValueReleaseEligibility

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FulfillmentResult:
    execution_id: str
    entitlement_id: str
    result_content_type: str
    result: Any
    result_hash: str
    result_size_bytes: int
    replayed_result: bool
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class FulfillmentOperation:
    status_code: int
    result: FulfillmentResult | None
    execution_id: str
    entitlement_id: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class FulfillmentRoute:
    fulfillment_config_id: str
    fulfillment_config_revision: int
    provider_type: FulfillmentProviderType
    endpoint_url: str
    endpoint_path: str
    request_timeout_seconds: int
    effective_timeout_seconds: float
    maximum_attempts: int


@dataclass(frozen=True, slots=True)
class ProtectedResourceDescriptor:
    merchant: object
    service: object
    input: object


class FulfillmentExpiryFinalizer:
    """Close expired nonterminal executions so paid failures cannot remain stranded."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._session = session
        self._clock = clock
        self._executions = FulfillmentExecutionRepository(session)
        self._compensations = CompensationApplicationService(session, clock=clock)

    async def finalize_one(self) -> FulfillmentExecution | None:
        now = self._read_clock()
        execution = await self._executions.claim_expired_recoverable_for_update(now=now)
        if execution is None:
            await self._session.commit()
            return None
        if FulfillmentExecutionState(execution.execution_state) is (
            FulfillmentExecutionState.PENDING
        ):
            # The database state invariant requires a terminal execution to record
            # one bounded processing attempt, even when expiry won before dispatch.
            execution.attempt_count = 1
            execution.started_at = now
        execution.execution_state = FulfillmentExecutionState.PERMANENT_FAILURE
        execution.failed_at = now
        execution.failure_code = "FULFILLMENT_ENTITLEMENT_EXPIRED"
        execution.compensation_required = True
        execution.lease_expires_at = None
        metadata: dict[str, object] = {
            "failure_code": execution.failure_code,
            "attempt_count": execution.attempt_count,
        }
        failed_event = self._event(
            execution,
            event_type=FulfillmentEventType.FULFILLMENT_FAILED,
            reason_code="FULFILLMENT_PERMANENT_FAILURE",
            metadata=metadata,
            occurred_at=now,
        )
        compensation_event = self._event(
            execution,
            event_type=FulfillmentEventType.COMPENSATION_REQUIRED,
            reason_code="FULFILLMENT_COMPENSATION_REQUIRED",
            metadata=metadata,
            occurred_at=now,
        )
        self._session.add(failed_event)
        await self._compensations.stage_for_permanent_failure(
            execution,
            occurred_at=now,
        )
        return await self._executions.update_with_event(
            execution,
            event=compensation_event,
        )

    @staticmethod
    def _event(
        execution: FulfillmentExecution,
        *,
        event_type: FulfillmentEventType,
        reason_code: str,
        metadata: dict[str, object],
        occurred_at: datetime,
    ) -> FulfillmentEvent:
        return FulfillmentEvent(
            id=new_fulfillment_event_id(),
            transaction_id=execution.transaction_id,
            entitlement_id=execution.entitlement_id,
            execution_id=execution.id,
            execution_revision=execution.revision + 1,
            event_type=event_type,
            actor_type=FulfillmentEventActorType.SYSTEM,
            actor_id=None,
            reason_code=reason_code,
            event_metadata=metadata,
            idempotency_key=(
                f"fulfillment:{execution.id}:{execution.revision + 1}:{event_type.value}"
            ),
            occurred_at=occurred_at,
        )

    def _read_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Fulfillment finalizer clock must be timezone-aware")
        return value.astimezone(UTC)


class FulfillmentApplicationService:
    """Turn one valid capability into at most one logical merchant execution."""

    def __init__(
        self,
        session: AsyncSession,
        capability_service: CapabilityTokenService,
        provider: FulfillmentProvider,
        *,
        payment_eligibility: PaymentValueReleaseEligibility,
        provider_base_url: str,
        execution_lease: timedelta,
        maximum_result_bytes: int,
        default_maximum_attempts: int = 3,
        clock: Clock = utc_now,
    ) -> None:
        if not timedelta(seconds=5) <= execution_lease <= timedelta(minutes=15):
            raise ValueError("Fulfillment execution lease is out of bounds")
        if not 1_024 <= maximum_result_bytes <= 1_048_576:
            raise ValueError("Fulfillment result size limit is out of bounds")
        if type(default_maximum_attempts) is not int or not 1 <= default_maximum_attempts <= 10:
            raise ValueError("Fulfillment default attempt limit is out of bounds")
        parsed_base = urlsplit(provider_base_url.rstrip("/"))
        if not parsed_base.scheme or not parsed_base.netloc:
            raise ValueError("Fulfillment provider base URL is invalid")
        self._session = session
        self._capabilities = capability_service
        self._provider = provider
        self._payment_eligibility = payment_eligibility
        self._provider_origin = (parsed_base.scheme.lower(), parsed_base.netloc.lower())
        self._execution_lease = execution_lease
        self._maximum_result_bytes = maximum_result_bytes
        self._default_maximum_attempts = default_maximum_attempts
        self._clock = clock
        self._entitlements = EntitlementRepository(session)
        self._compensation_cases = CompensationCaseRepository(session)
        self._compensations = CompensationApplicationService(session, clock=clock)
        self._executions = FulfillmentExecutionRepository(session)
        self._merchants = MerchantRepository(session)
        self._quotes = QuoteRepository(session)
        self._services = ServiceRepository(session)
        self._configs = ServiceFulfillmentConfigRepository(session)

    async def describe_resource(
        self,
        *,
        merchant_slug: str,
        service_slug: str,
        input_value: object,
    ) -> ProtectedResourceDescriptor:
        """Resolve and validate the quote request represented by a 402 challenge."""
        merchant = await self._merchants.get_by_slug(merchant_slug)
        if merchant is None:
            raise ResourceNotFoundError("Merchant", merchant_slug)
        service = await self._services.get_by_slug(merchant.id, service_slug)
        if service is None:
            raise ResourceNotFoundError("Service", service_slug)
        try:
            canonical_input = validate_normalize_and_hash_service_input(
                input_value,
                service.input_schema,
            )
        except ServiceInputValueError as error:
            raise QuoteInputValidationError(
                tuple(
                    QuoteInputIssue(path=path, keyword=keyword)
                    for path, keyword in error.violations
                )
            ) from error
        except ServiceInputConfigurationError as error:
            raise ServiceConfigurationError(service.id) from error
        return ProtectedResourceDescriptor(
            merchant=merchant,
            service=service,
            input=canonical_input.value,
        )

    async def reconcile_execution(self, execution_id: str) -> FulfillmentOperation:
        """Retry one existing logical execution through the normal capability path."""
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise EntitlementNotFoundError(
                "The fulfillment execution was not found",
                "FULFILLMENT_EXECUTION_NOT_FOUND",
            )
        if FulfillmentExecutionState(execution.execution_state) not in {
            FulfillmentExecutionState.RETRYABLE_FAILURE,
            FulfillmentExecutionState.RECONCILIATION_REQUIRED,
        }:
            raise FulfillmentConflictError(
                "The fulfillment execution is not recoverable",
                "FULFILLMENT_RECONCILIATION_NOT_ALLOWED",
            )
        entitlement = await self._entitlements.get(execution.entitlement_id)
        # The quote is addressed by the entitlement, never by operator input.
        quote = await self._quotes.get(entitlement.quote_id) if entitlement is not None else None
        merchant = await self._merchants.get(execution.merchant_id)
        service = await self._services.get(execution.service_id)
        if entitlement is None or quote is None or merchant is None or service is None:
            raise FulfillmentIntegrityError(
                "The fulfillment recovery binding is incomplete",
                "FULFILLMENT_INTEGRITY_FAILED",
            )
        issued = self._capabilities.issue(entitlement)
        return await self.execute(
            merchant_slug=merchant.slug,
            service_slug=service.slug,
            input_value=quote.input,
            token=issued.token,
            allow_reconciliation=True,
        )

    async def execute(
        self,
        *,
        merchant_slug: str,
        service_slug: str,
        input_value: object,
        token: str,
        allow_reconciliation: bool = False,
    ) -> FulfillmentOperation:
        claims = self._capabilities.verify(token)
        entitlement = await self._entitlements.get(claims.entitlement_id)
        if entitlement is None:
            raise EntitlementNotFoundError(
                "The capability entitlement was not found",
                "ENTITLEMENT_NOT_FOUND",
            )
        EntitlementApplicationService.verify_integrity(entitlement)
        now = self._read_clock()
        if self._as_utc(entitlement.expires_at) <= now:
            raise EntitlementExpiredError(
                "The entitlement has expired",
                "ENTITLEMENT_EXPIRED",
            )
        quote = await self._quotes.get(entitlement.quote_id)
        if quote is None:
            raise FulfillmentIntegrityError(
                "The paid quote contract was not found",
                "FULFILLMENT_INTEGRITY_FAILED",
            )
        try:
            quote_integrity = verify_quote_integrity(quote)
        except IntegrityStructureError as error:
            raise FulfillmentIntegrityError(
                "The paid quote contract is malformed",
                "FULFILLMENT_INTEGRITY_FAILED",
            ) from error
        if not quote_integrity.hash_matches or not self._quote_matches_entitlement(
            quote,
            entitlement,
        ):
            raise FulfillmentIntegrityError(
                "The paid quote contract failed integrity verification",
                "FULFILLMENT_INTEGRITY_FAILED",
            )
        paid_service_contract = quote.service_snapshot["service"]
        merchant = await self._merchants.get_by_slug(merchant_slug)
        if merchant is None:
            raise CapabilityForbiddenError(
                "The capability does not bind this merchant",
                "CAPABILITY_RESOURCE_MISMATCH",
            )
        service = await self._services.get_by_slug(merchant.id, service_slug)
        if service is None:
            raise CapabilityForbiddenError(
                "The capability does not bind this service",
                "CAPABILITY_RESOURCE_MISMATCH",
            )
        try:
            canonical_input = validate_normalize_and_hash_service_input(
                input_value,
                paid_service_contract["input_schema"],
            )
        except ServiceInputValueError as error:
            raise CapabilityForbiddenError(
                "The request input does not match the paid resource",
                "CAPABILITY_INPUT_MISMATCH",
            ) from error
        except ServiceInputConfigurationError as error:
            raise FulfillmentIntegrityError(
                "The service input contract is invalid",
                "FULFILLMENT_SERVICE_CONFIGURATION_INVALID",
            ) from error
        self._capabilities.require_binding(
            claims,
            entitlement_id=entitlement.id,
            transaction_id=entitlement.transaction_id,
            merchant_id=merchant.id,
            service_id=service.id,
            quote_id=entitlement.quote_id,
            quote_hash=entitlement.quote_hash,
            input_hash=canonical_input.input_hash,
        )
        if claims.account_id != entitlement.account_id:
            raise CapabilityForbiddenError(
                "The capability account binding is invalid",
                "CAPABILITY_RESOURCE_MISMATCH",
            )
        if not compare_digest(canonical_input.input_hash, entitlement.input_hash):
            raise CapabilityForbiddenError(
                "The request input does not match the paid input",
                "CAPABILITY_INPUT_MISMATCH",
            )
        config = await self._configs.get_by_service_id(service.id)
        maximum_attempts = self._configured_maximum_attempts(config)
        # Replays and already-claimed responses are value release too. Fence them
        # from any locally known refund or unresolved payment anomaly.
        await self._payment_eligibility.require_local_value_release_eligibility(
            entitlement.transaction_id,
            for_update=True,
        )
        await self._require_no_compensation_quarantine(entitlement.transaction_id)
        claimed = await self._claim_execution(
            entitlement,
            maximum_attempts=maximum_attempts,
            allow_reconciliation=allow_reconciliation,
        )
        if isinstance(claimed, FulfillmentResult):
            return FulfillmentOperation(
                status_code=200,
                result=claimed,
                execution_id=claimed.execution_id,
                entitlement_id=claimed.entitlement_id,
                reason_code="FULFILLMENT_RESULT_REPLAYED",
            )
        execution, owns_lease = claimed
        if not owns_lease:
            return FulfillmentOperation(
                status_code=202,
                result=None,
                execution_id=execution.id,
                entitlement_id=entitlement.id,
                reason_code="FULFILLMENT_ALREADY_CLAIMED",
            )

        expected_generation = execution.lease_generation
        maximum_attempts = self._execution_maximum_attempts(
            execution,
            fallback=maximum_attempts,
        )
        route = self._execution_route(config, quote, execution=execution)
        if route is None:
            failure_code = (
                "FULFILLMENT_PROVIDER_UNAVAILABLE"
                if config is None or getattr(config, "enabled", None) is not True
                else "FULFILLMENT_PROVIDER_CONFIGURATION_INVALID"
            )
            terminal = await self._record_failure(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                maximum_attempts=maximum_attempts,
                failure_code=failure_code,
                retryable=True,
            )
            if terminal is None:
                raise FulfillmentConflictError(
                    "The fulfillment execution lease was lost",
                    "FULFILLMENT_ALREADY_CLAIMED",
                )
            if terminal:
                raise FulfillmentPermanentError(
                    "Fulfillment retries are exhausted and compensation is required",
                    "FULFILLMENT_COMPENSATION_REQUIRED",
                )
            raise FulfillmentRetryableError(
                "The merchant fulfillment route is temporarily unavailable",
                "FULFILLMENT_RETRYABLE_FAILURE",
            )
        try:
            # Every lease owner obtains its own authoritative provider proof.
            # This happens after the locked claim decision so a concurrent state
            # transition cannot turn a skipped proof into a redispatch.
            await self._payment_eligibility.verify_transaction_for_value_release(
                entitlement.transaction_id
            )
            dispatch = await self._record_request_sent(
                execution,
                entitlement=entitlement,
                quote=quote,
                expected_generation=expected_generation,
            )
        except (
            EntitlementConflictError,
            EntitlementIntegrityError,
            EntitlementUnavailableError,
        ):
            # No merchant request was emitted. Release the lease without making
            # this payment fence consume a logical merchant attempt.
            await self._record_failure(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                maximum_attempts=maximum_attempts,
                failure_code="FULFILLMENT_PAYMENT_REVERIFICATION_BLOCKED",
                retryable=True,
                exhausts_attempts=False,
            )
            raise
        if dispatch is None:
            terminal = await self._record_failure(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                maximum_attempts=maximum_attempts,
                failure_code="FULFILLMENT_PROVIDER_UNAVAILABLE",
                retryable=True,
            )
            if terminal is None:
                raise FulfillmentConflictError(
                    "The fulfillment execution lease was lost",
                    "FULFILLMENT_ALREADY_CLAIMED",
                )
            if terminal:
                raise FulfillmentPermanentError(
                    "Fulfillment retries are exhausted and compensation is required",
                    "FULFILLMENT_COMPENSATION_REQUIRED",
                )
            raise FulfillmentRetryableError(
                "The merchant fulfillment route was disabled before dispatch",
                "FULFILLMENT_RETRYABLE_FAILURE",
            )
        execution, route = dispatch
        maximum_attempts = route.maximum_attempts
        try:
            provider_result = await self._provider.execute(
                MerchantFulfillmentRequest(
                    fulfillment_execution_id=execution.id,
                    service_id=service.id,
                    service_slug=paid_service_contract["slug"],
                    input=canonical_input.value,
                    input_hash=canonical_input.input_hash,
                    request_timeout_seconds=route.effective_timeout_seconds,
                ),
                endpoint_path=route.endpoint_path,
            )
        except MerchantFulfillmentInProgressError as error:
            recorded = await self._record_failure(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                maximum_attempts=maximum_attempts,
                failure_code=error.reason_code,
                retryable=True,
                exhausts_attempts=False,
            )
            if recorded is None:
                raise FulfillmentConflictError(
                    "The fulfillment execution lease was lost",
                    "FULFILLMENT_ALREADY_CLAIMED",
                ) from error
            return FulfillmentOperation(
                status_code=202,
                result=None,
                execution_id=execution.id,
                entitlement_id=entitlement.id,
                reason_code="FULFILLMENT_PROVIDER_IN_PROGRESS",
            )
        except MerchantFulfillmentRetryableError as error:
            terminal = await self._record_failure(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                maximum_attempts=maximum_attempts,
                failure_code=error.reason_code,
                retryable=True,
            )
            if terminal is None:
                raise FulfillmentConflictError(
                    "The fulfillment execution lease was lost",
                    "FULFILLMENT_ALREADY_CLAIMED",
                ) from error
            if terminal:
                raise FulfillmentPermanentError(
                    "Fulfillment retries are exhausted and compensation is required",
                    "FULFILLMENT_COMPENSATION_REQUIRED",
                ) from error
            raise FulfillmentRetryableError(
                "Merchant fulfillment may be retried with the same capability",
                "FULFILLMENT_RETRYABLE_FAILURE",
            ) from error
        except MerchantFulfillmentPermanentError as error:
            recorded = await self._record_failure(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                maximum_attempts=maximum_attempts,
                failure_code=error.reason_code,
                retryable=False,
            )
            if recorded is None:
                raise FulfillmentConflictError(
                    "The fulfillment execution lease was lost",
                    "FULFILLMENT_ALREADY_CLAIMED",
                ) from error
            raise FulfillmentPermanentError(
                "Merchant fulfillment failed permanently",
                "FULFILLMENT_COMPENSATION_REQUIRED",
            ) from error
        except MerchantFulfillmentResponseError as error:
            await self._record_reconciliation_required(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                failure_code=error.reason_code,
            )
            raise FulfillmentIntegrityError(
                "The merchant response failed validation",
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
            ) from error

        provider_content_type = normalize_json_content_type(provider_result.result_content_type)
        contract_content_type = normalize_json_content_type(
            paid_service_contract["output_content_type"]
        )
        if provider_content_type is None or provider_content_type != contract_content_type:
            await self._record_reconciliation_required(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                failure_code="FULFILLMENT_RESULT_CONTENT_TYPE_MISMATCH",
            )
            raise FulfillmentIntegrityError(
                "The merchant result content type does not match the service contract",
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
            )
        try:
            output_violations = validate_json_instance(
                provider_result.result,
                paid_service_contract["output_schema"],
            )
        except JSONSchemaConfigurationError as error:
            await self._record_reconciliation_required(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                failure_code="FULFILLMENT_SERVICE_OUTPUT_SCHEMA_INVALID",
            )
            raise FulfillmentIntegrityError(
                "The service output contract is invalid",
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
            ) from error
        if output_violations:
            await self._record_reconciliation_required(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                failure_code="FULFILLMENT_RESULT_SCHEMA_INVALID",
            )
            raise FulfillmentIntegrityError(
                "The merchant result does not match the service output contract",
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
            )

        try:
            result_bytes = canonical_json_bytes(provider_result.result)
        except CanonicalJSONError as error:
            await self._record_reconciliation_required(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                failure_code="FULFILLMENT_PROVIDER_RESPONSE_INVALID",
            )
            raise FulfillmentIntegrityError(
                "The merchant result cannot be normalized",
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
            ) from error
        if len(result_bytes) > self._maximum_result_bytes:
            await self._record_reconciliation_required(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                failure_code="FULFILLMENT_RESULT_TOO_LARGE",
            )
            raise FulfillmentIntegrityError(
                "The merchant result exceeds the configured limit",
                "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
            )
        # Linearize final value release behind the payment aggregate once more.
        # A refund/reconciliation webhook that committed while the merchant ran
        # must win before the result can be durably persisted or returned.
        await self._payment_eligibility.require_local_value_release_eligibility(
            entitlement.transaction_id,
            for_update=True,
        )
        return FulfillmentOperation(
            status_code=200,
            result=await self._record_success(
                entitlement,
                execution_id=execution.id,
                expected_generation=expected_generation,
                result=provider_result.result,
                result_content_type=provider_content_type,
                result_bytes=result_bytes,
            ),
            execution_id=execution.id,
            entitlement_id=entitlement.id,
            reason_code="FULFILLMENT_SUCCEEDED",
        )

    async def _claim_execution(
        self,
        entitlement: Entitlement,
        *,
        maximum_attempts: int,
        allow_reconciliation: bool = False,
    ) -> tuple[FulfillmentExecution, bool] | FulfillmentResult:
        execution = await self._executions.get_by_entitlement_id_for_update(entitlement.id)
        if execution is None:
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
                execution_state=FulfillmentExecutionState.PENDING,
                attempt_count=0,
                started_at=None,
                completed_at=None,
                failed_at=None,
                result_content_type=None,
                result_json=None,
                result_hash=None,
                result_size_bytes=None,
                failure_code=None,
                compensation_required=False,
                revision=1,
                lease_generation=0,
                lease_expires_at=None,
            )
            claimed_event = self._execution_event(
                execution,
                event_type=FulfillmentEventType.FULFILLMENT_CLAIMED,
                reason_code="FULFILLMENT_CLAIMED",
                revision=1,
                metadata={},
            )
            try:
                execution = await self._executions.create_with_event(
                    execution,
                    event=claimed_event,
                )
            except IntegrityError:
                await self._session.rollback()
            execution = await self._executions.get_by_entitlement_id_for_update(entitlement.id)
            if execution is None:
                raise FulfillmentIntegrityError(
                    "The fulfillment claim was not persisted",
                    "FULFILLMENT_INTEGRITY_FAILED",
                )

        await self._require_no_compensation_quarantine(
            entitlement.transaction_id,
            execution=execution,
        )

        state = FulfillmentExecutionState(execution.execution_state)
        if state is FulfillmentExecutionState.SUCCEEDED:
            return self._stored_result(execution, replayed=True)
        if state is FulfillmentExecutionState.PERMANENT_FAILURE:
            raise FulfillmentPermanentError(
                "Fulfillment failed permanently and requires compensation",
                "FULFILLMENT_COMPENSATION_REQUIRED",
            )
        if state is FulfillmentExecutionState.RECONCILIATION_REQUIRED and not allow_reconciliation:
            raise FulfillmentIntegrityError(
                "Fulfillment requires reconciliation",
                "FULFILLMENT_RECONCILIATION_REQUIRED",
            )
        now = self._read_clock()
        if state is FulfillmentExecutionState.EXECUTING:
            if (
                execution.lease_expires_at is not None
                and self._as_utc(execution.lease_expires_at) > now
            ):
                await self._session.commit()
                return execution, False
            execution.execution_state = FulfillmentExecutionState.RETRYABLE_FAILURE
            execution.failed_at = now
            execution.failure_code = "FULFILLMENT_EXECUTION_LEASE_EXPIRED"
            execution.lease_expires_at = None
            stale_event = self._execution_event(
                execution,
                event_type=FulfillmentEventType.FULFILLMENT_RETRY_SCHEDULED,
                reason_code="FULFILLMENT_EXECUTION_LEASE_EXPIRED",
                revision=execution.revision + 1,
                metadata={"attempt_count": execution.attempt_count},
            )
            self._session.add(
                self._execution_event(
                    execution,
                    event_type=FulfillmentEventType.FULFILLMENT_FAILED,
                    reason_code="FULFILLMENT_RETRYABLE_FAILURE",
                    revision=execution.revision + 1,
                    metadata={
                        "failure_code": execution.failure_code,
                        "attempt_count": execution.attempt_count,
                    },
                )
            )
            execution = await self._executions.update_with_event(
                execution,
                event=stale_event,
            )
            execution = await self._executions.get_by_entitlement_id_for_update(entitlement.id)
            assert execution is not None
        resumes_in_progress_attempt = state is FulfillmentExecutionState.RETRYABLE_FAILURE and (
            execution.failure_code
            in {
                "ORBITINTEL_EXECUTION_IN_PROGRESS",
                "FULFILLMENT_PAYMENT_REVERIFICATION_BLOCKED",
            }
        )
        effective_maximum_attempts = self._execution_maximum_attempts(
            execution,
            fallback=maximum_attempts,
        )
        if (
            execution.attempt_count >= effective_maximum_attempts
            and not resumes_in_progress_attempt
        ):
            execution.execution_state = FulfillmentExecutionState.PERMANENT_FAILURE
            execution.failed_at = now
            execution.failure_code = "FULFILLMENT_RETRY_EXHAUSTED"
            execution.compensation_required = True
            execution.lease_expires_at = None
            failed_event = self._execution_event(
                execution,
                event_type=FulfillmentEventType.COMPENSATION_REQUIRED,
                reason_code="FULFILLMENT_COMPENSATION_REQUIRED",
                revision=execution.revision + 1,
                metadata={"failure_code": execution.failure_code},
            )
            self._session.add(
                self._execution_event(
                    execution,
                    event_type=FulfillmentEventType.FULFILLMENT_FAILED,
                    reason_code="FULFILLMENT_PERMANENT_FAILURE",
                    revision=execution.revision + 1,
                    metadata={
                        "failure_code": execution.failure_code,
                        "attempt_count": execution.attempt_count,
                    },
                )
            )
            await self._compensations.stage_for_permanent_failure(
                execution,
                occurred_at=now,
            )
            await self._executions.update_with_event(execution, event=failed_event)
            raise FulfillmentPermanentError(
                "Fulfillment retries are exhausted and compensation is required",
                "FULFILLMENT_COMPENSATION_REQUIRED",
            )
        execution.execution_state = FulfillmentExecutionState.EXECUTING
        if not resumes_in_progress_attempt:
            execution.attempt_count += 1
        execution.started_at = execution.started_at or now
        execution.failed_at = None
        execution.failure_code = None
        execution.compensation_required = False
        execution.lease_generation += 1
        execution.lease_expires_at = now + self._execution_lease
        started_event = self._execution_event(
            execution,
            event_type=FulfillmentEventType.FULFILLMENT_STARTED,
            reason_code="FULFILLMENT_STARTED",
            revision=execution.revision + 1,
            metadata={
                "attempt_count": execution.attempt_count,
                "lease_generation": execution.lease_generation,
            },
        )
        execution = await self._executions.update_with_event(
            execution,
            event=started_event,
        )
        return execution, True

    @staticmethod
    def _quote_matches_entitlement(quote: object, entitlement: Entitlement) -> bool:
        return (
            quote.id == entitlement.quote_id
            and compare_digest(quote.quote_hash, entitlement.quote_hash)
            and quote.merchant_id == entitlement.merchant_id
            and quote.service_id == entitlement.service_id
            and compare_digest(quote.input_hash, entitlement.input_hash)
            and quote.amount == entitlement.amount
            and quote.currency == entitlement.currency
            and quote.purchase_type == entitlement.purchase_type
        )

    async def _record_request_sent(
        self,
        execution: FulfillmentExecution,
        *,
        entitlement: Entitlement,
        quote: object,
        expected_generation: int,
    ) -> tuple[FulfillmentExecution, FulfillmentRoute] | None:
        # Hold the payment row lock through the request-sent commit. This commit
        # is the authorization linearization point immediately before merchant I/O.
        await self._payment_eligibility.require_local_value_release_eligibility(
            execution.transaction_id,
            for_update=True,
        )
        locked = await self._executions.get_by_entitlement_id_for_update(execution.entitlement_id)
        if locked is None or locked.id != execution.id:
            raise FulfillmentIntegrityError(
                "The fulfillment execution was not found",
                "FULFILLMENT_NOT_FOUND",
            )
        await self._require_no_compensation_quarantine(
            execution.transaction_id,
            execution=locked,
        )
        now = self._read_clock()
        if (
            FulfillmentExecutionState(locked.execution_state)
            is not FulfillmentExecutionState.EXECUTING
            or locked.lease_generation != expected_generation
            or locked.lease_expires_at is None
        ):
            await self._session.rollback()
            raise FulfillmentConflictError(
                "The fulfillment execution lease was lost",
                "FULFILLMENT_ALREADY_CLAIMED",
            )
        if self._as_utc(entitlement.expires_at) <= now:
            await self._session.rollback()
            raise EntitlementExpiredError(
                "The entitlement expired during payment re-verification",
                "ENTITLEMENT_EXPIRED",
            )
        config = await self._configs.get_by_service_id_for_update(locked.service_id)
        route = self._execution_route(config, quote, execution=locked)
        if route is None:
            await self._session.rollback()
            return None
        if locked.fulfillment_config_id is None:
            locked.fulfillment_config_id = route.fulfillment_config_id
            locked.fulfillment_config_revision = route.fulfillment_config_revision
            locked.provider_type = route.provider_type
            locked.endpoint_url = route.endpoint_url
            locked.request_timeout_seconds = route.request_timeout_seconds
            locked.maximum_attempts = route.maximum_attempts
        elif not self._route_matches_execution(route, locked):
            await self._session.rollback()
            raise FulfillmentIntegrityError(
                "The fulfillment route does not match its immutable snapshot",
                "FULFILLMENT_INTEGRITY_FAILED",
            )
        # Provider re-verification runs outside the execution lock and may use a
        # meaningful portion of the original claim lease. Renew only while this
        # generation is still current, then keep merchant I/O one second inside it.
        locked.lease_expires_at = now + self._execution_lease
        event = self._execution_event(
            locked,
            event_type=FulfillmentEventType.MERCHANT_REQUEST_SENT,
            reason_code="MERCHANT_REQUEST_SENT",
            revision=locked.revision + 1,
            metadata={
                "attempt_count": locked.attempt_count,
                "lease_generation": locked.lease_generation,
                "lease_expires_at": locked.lease_expires_at.isoformat(),
                "fulfillment_config_id": route.fulfillment_config_id,
                "fulfillment_config_revision": route.fulfillment_config_revision,
            },
        )
        recorded = await self._executions.update_with_event(locked, event=event)
        return recorded, route

    async def _record_success(
        self,
        entitlement: Entitlement,
        *,
        execution_id: str,
        expected_generation: int,
        result: Any,
        result_content_type: str,
        result_bytes: bytes,
    ) -> FulfillmentResult:
        execution = await self._executions.get_by_entitlement_id_for_update(entitlement.id)
        if execution is None or execution.id != execution_id:
            raise FulfillmentIntegrityError(
                "The fulfillment execution was not found",
                "FULFILLMENT_NOT_FOUND",
            )
        await self._require_no_compensation_quarantine(
            entitlement.transaction_id,
            execution=execution,
        )
        if FulfillmentExecutionState(execution.execution_state) is (
            FulfillmentExecutionState.SUCCEEDED
        ):
            return self._stored_result(execution, replayed=True)
        now = self._read_clock()
        if (
            FulfillmentExecutionState(execution.execution_state)
            is not FulfillmentExecutionState.EXECUTING
            or execution.lease_generation != expected_generation
            or execution.lease_expires_at is None
            or self._as_utc(execution.lease_expires_at) <= now
        ):
            await self._session.rollback()
            raise FulfillmentConflictError(
                "The fulfillment execution lease was lost",
                "FULFILLMENT_ALREADY_CLAIMED",
            )
        execution.execution_state = FulfillmentExecutionState.SUCCEEDED
        execution.completed_at = now
        execution.failed_at = None
        execution.result_content_type = result_content_type
        # JSON.NULL distinguishes a legitimate JSON null result from the SQL
        # NULL used for executions that have no result yet.
        execution.result_json = JSON.NULL if result is None else result
        execution.result_hash = sha256_bytes(result_bytes)
        execution.result_size_bytes = len(result_bytes)
        execution.failure_code = None
        execution.compensation_required = False
        execution.lease_expires_at = None
        response_event = self._execution_event(
            execution,
            event_type=FulfillmentEventType.MERCHANT_RESPONSE_RECEIVED,
            reason_code="MERCHANT_RESULT_RECEIVED",
            revision=execution.revision + 1,
            metadata={
                "result_content_type": result_content_type,
                "result_size_bytes": len(result_bytes),
                "result_hash": execution.result_hash,
            },
        )
        success_event = self._execution_event(
            execution,
            event_type=FulfillmentEventType.FULFILLMENT_SUCCEEDED,
            reason_code="FULFILLMENT_SUCCEEDED",
            revision=execution.revision + 1,
            metadata={
                "result_hash": execution.result_hash,
                "result_size_bytes": len(result_bytes),
            },
        )
        self._session.add(response_event)
        execution = await self._executions.update_with_event(
            execution,
            event=success_event,
        )
        return self._stored_result(execution, replayed=False)

    async def _require_no_compensation_quarantine(
        self,
        transaction_id: str,
        *,
        execution: FulfillmentExecution | None = None,
    ) -> None:
        case = await self._compensation_cases.get_by_transaction_id(transaction_id)
        if case is None and (execution is None or not execution.compensation_required):
            return
        raise FulfillmentPermanentError(
            "Compensation evidence quarantines paid entitlement access",
            "ENTITLEMENT_COMPENSATION_QUARANTINED",
        )

    async def _record_failure(
        self,
        entitlement: Entitlement,
        *,
        execution_id: str,
        expected_generation: int,
        maximum_attempts: int,
        failure_code: str,
        retryable: bool,
        exhausts_attempts: bool = True,
    ) -> bool | None:
        execution = await self._executions.get_by_entitlement_id_for_update(entitlement.id)
        if (
            execution is None
            or execution.id != execution_id
            or execution.lease_generation != expected_generation
            or FulfillmentExecutionState(execution.execution_state)
            is not FulfillmentExecutionState.EXECUTING
        ):
            await self._session.rollback()
            return None
        now = self._read_clock()
        if execution.lease_expires_at is None or self._as_utc(execution.lease_expires_at) <= now:
            await self._session.rollback()
            return None
        terminal = not retryable or (
            exhausts_attempts and execution.attempt_count >= maximum_attempts
        )
        execution.execution_state = (
            FulfillmentExecutionState.PERMANENT_FAILURE
            if terminal
            else FulfillmentExecutionState.RETRYABLE_FAILURE
        )
        execution.failed_at = now
        execution.failure_code = self._safe_failure_code(failure_code)
        execution.compensation_required = terminal
        execution.lease_expires_at = None
        metadata: dict[str, object] = {
            "failure_code": execution.failure_code,
            "attempt_count": execution.attempt_count,
        }
        failed_event = self._execution_event(
            execution,
            event_type=FulfillmentEventType.FULFILLMENT_FAILED,
            reason_code=(
                "FULFILLMENT_PERMANENT_FAILURE" if terminal else "FULFILLMENT_RETRYABLE_FAILURE"
            ),
            revision=execution.revision + 1,
            metadata=metadata,
        )
        recovery_event = self._execution_event(
            execution,
            event_type=(
                FulfillmentEventType.COMPENSATION_REQUIRED
                if terminal
                else FulfillmentEventType.FULFILLMENT_RETRY_SCHEDULED
            ),
            reason_code=(
                "FULFILLMENT_COMPENSATION_REQUIRED" if terminal else "FULFILLMENT_RETRYABLE_FAILURE"
            ),
            revision=execution.revision + 1,
            metadata=metadata,
        )
        self._session.add(failed_event)
        if terminal:
            await self._compensations.stage_for_permanent_failure(
                execution,
                occurred_at=now,
            )
        await self._executions.update_with_event(execution, event=recovery_event)
        return terminal

    async def _record_reconciliation_required(
        self,
        entitlement: Entitlement,
        *,
        execution_id: str,
        expected_generation: int,
        failure_code: str,
    ) -> None:
        execution = await self._executions.get_by_entitlement_id_for_update(entitlement.id)
        if (
            execution is None
            or execution.id != execution_id
            or execution.lease_generation != expected_generation
            or FulfillmentExecutionState(execution.execution_state)
            is not FulfillmentExecutionState.EXECUTING
        ):
            await self._session.rollback()
            return
        now = self._read_clock()
        if execution.lease_expires_at is None or self._as_utc(execution.lease_expires_at) <= now:
            await self._session.rollback()
            return
        execution.execution_state = FulfillmentExecutionState.RECONCILIATION_REQUIRED
        execution.failed_at = now
        execution.failure_code = self._safe_failure_code(failure_code)
        execution.compensation_required = False
        execution.lease_expires_at = None
        event = self._execution_event(
            execution,
            event_type=FulfillmentEventType.FULFILLMENT_FAILED,
            reason_code="FULFILLMENT_RECONCILIATION_REQUIRED",
            revision=execution.revision + 1,
            metadata={"failure_code": execution.failure_code},
        )
        await self._executions.update_with_event(execution, event=event)

    def _stored_result(
        self,
        execution: FulfillmentExecution,
        *,
        replayed: bool,
    ) -> FulfillmentResult:
        if (
            normalize_json_content_type(execution.result_content_type) is None
            or execution.result_hash is None
            or execution.result_size_bytes is None
            or execution.completed_at is None
        ):
            raise FulfillmentIntegrityError(
                "The stored fulfillment result is incomplete",
                "FULFILLMENT_INTEGRITY_FAILED",
            )
        result: Any = None if execution.result_json is JSON.NULL else execution.result_json
        try:
            result_bytes = canonical_json_bytes(result)
        except CanonicalJSONError as error:
            raise FulfillmentIntegrityError(
                "The stored fulfillment result is invalid",
                "FULFILLMENT_INTEGRITY_FAILED",
            ) from error
        if (
            len(result_bytes) != execution.result_size_bytes
            or len(result_bytes) > self._maximum_result_bytes
            or not compare_digest(sha256_bytes(result_bytes), execution.result_hash)
        ):
            raise FulfillmentIntegrityError(
                "The stored fulfillment result failed integrity verification",
                "FULFILLMENT_INTEGRITY_FAILED",
            )
        return FulfillmentResult(
            execution_id=execution.id,
            entitlement_id=execution.entitlement_id,
            result_content_type=execution.result_content_type,
            result=result,
            result_hash=execution.result_hash,
            result_size_bytes=execution.result_size_bytes,
            replayed_result=replayed,
            completed_at=self._as_utc(execution.completed_at),
        )

    def _execution_event(
        self,
        execution: FulfillmentExecution,
        *,
        event_type: FulfillmentEventType,
        reason_code: str,
        revision: int,
        metadata: dict[str, object],
    ) -> FulfillmentEvent:
        return FulfillmentEvent(
            id=new_fulfillment_event_id(),
            transaction_id=execution.transaction_id,
            entitlement_id=execution.entitlement_id,
            execution_id=execution.id,
            execution_revision=revision,
            event_type=event_type,
            actor_type=FulfillmentEventActorType.RESOURCE_GATEWAY,
            actor_id=None,
            reason_code=reason_code,
            event_metadata=metadata,
            idempotency_key=(f"fulfillment:{execution.id}:{revision}:{event_type.value}"),
            occurred_at=self._read_clock(),
        )

    def _configured_maximum_attempts(self, config: object | None) -> int:
        if config is not None:
            configured = getattr(config, "maximum_attempts", None)
            if type(configured) is int and 1 <= configured <= 10:
                return configured
        return self._default_maximum_attempts

    @staticmethod
    def _execution_maximum_attempts(
        execution: FulfillmentExecution,
        *,
        fallback: int,
    ) -> int:
        pinned = execution.maximum_attempts
        if pinned is None:
            return fallback
        if type(pinned) is not int or not 1 <= pinned <= 10:
            raise FulfillmentIntegrityError(
                "The fulfillment route snapshot has an invalid retry limit",
                "FULFILLMENT_INTEGRITY_FAILED",
            )
        return pinned

    def _execution_route(
        self,
        config: object | None,
        quote: object,
        *,
        execution: FulfillmentExecution,
    ) -> FulfillmentRoute | None:
        """Resolve a bounded route only after a durable execution has been claimed."""
        snapshot_values = (
            execution.fulfillment_config_id,
            execution.fulfillment_config_revision,
            execution.provider_type,
            execution.endpoint_url,
            execution.request_timeout_seconds,
            execution.maximum_attempts,
        )
        if any(value is not None for value in snapshot_values):
            if any(value is None for value in snapshot_values):
                raise FulfillmentIntegrityError(
                    "The fulfillment route snapshot is incomplete",
                    "FULFILLMENT_INTEGRITY_FAILED",
                )
            if (
                config is None
                or getattr(config, "enabled", None) is not True
                or getattr(config, "id", None) != execution.fulfillment_config_id
            ):
                return None
            config_id = execution.fulfillment_config_id
            config_revision = execution.fulfillment_config_revision
            provider_type_value = execution.provider_type
            endpoint_url = execution.endpoint_url
            request_timeout = execution.request_timeout_seconds
            maximum_attempts = execution.maximum_attempts
        else:
            if config is None or getattr(config, "enabled", None) is not True:
                return None
            config_id = getattr(config, "id", None)
            config_revision = getattr(config, "revision", None)
            provider_type_value = getattr(config, "provider_type", None)
            endpoint_url = getattr(config, "endpoint_url", None)
            request_timeout = getattr(config, "request_timeout_seconds", None)
            maximum_attempts = getattr(config, "maximum_attempts", None)
        quote_timeout = getattr(quote, "maximum_fulfillment_seconds", None)
        try:
            provider_type = FulfillmentProviderType(provider_type_value)
        except (TypeError, ValueError):
            return None
        if (
            not isinstance(config_id, str)
            or not config_id.startswith("sfc_")
            or type(config_revision) is not int
            or config_revision < 1
            or provider_type is not FulfillmentProviderType.HTTP
            or type(maximum_attempts) is not int
            or not 1 <= maximum_attempts <= 10
            or type(request_timeout) is not int
            or not 1 <= request_timeout <= 60
            or type(quote_timeout) is not int
            or quote_timeout < 1
            or not isinstance(endpoint_url, str)
        ):
            return None
        try:
            endpoint_path = self._endpoint_path(endpoint_url)
        except (FulfillmentIntegrityError, TypeError, ValueError):
            return None
        lease_budget = self._execution_lease.total_seconds() - 1.0
        hard_timeout = min(float(request_timeout), float(quote_timeout), lease_budget)
        if hard_timeout <= 0:
            return None
        return FulfillmentRoute(
            fulfillment_config_id=config_id,
            fulfillment_config_revision=config_revision,
            provider_type=provider_type,
            endpoint_url=endpoint_url,
            endpoint_path=endpoint_path,
            request_timeout_seconds=request_timeout,
            effective_timeout_seconds=hard_timeout,
            maximum_attempts=maximum_attempts,
        )

    @staticmethod
    def _route_matches_execution(
        route: FulfillmentRoute,
        execution: FulfillmentExecution,
    ) -> bool:
        return (
            execution.fulfillment_config_id == route.fulfillment_config_id
            and execution.fulfillment_config_revision == route.fulfillment_config_revision
            and FulfillmentProviderType(execution.provider_type) is route.provider_type
            and execution.endpoint_url == route.endpoint_url
            and execution.request_timeout_seconds == route.request_timeout_seconds
            and execution.maximum_attempts == route.maximum_attempts
        )

    def _endpoint_path(self, endpoint_url: str) -> str:
        parsed = urlsplit(endpoint_url)
        if (parsed.scheme.lower(), parsed.netloc.lower()) != self._provider_origin:
            raise FulfillmentIntegrityError(
                "The merchant endpoint origin does not match the configured provider",
                "FULFILLMENT_SERVICE_CONFIGURATION_INVALID",
            )
        if not parsed.path.startswith("/") or parsed.path.endswith("/"):
            raise FulfillmentIntegrityError(
                "The merchant endpoint path is invalid",
                "FULFILLMENT_SERVICE_CONFIGURATION_INVALID",
            )
        return parsed.path

    @staticmethod
    def _safe_failure_code(value: str) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", value) is None:
            return "FULFILLMENT_PROVIDER_FAILURE"
        return value

    def _read_clock(self) -> datetime:
        return self._as_utc(self._clock())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Fulfillment timestamps must be timezone-aware")
        return value.astimezone(UTC)
