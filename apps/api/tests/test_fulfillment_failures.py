"""Failure, recovery, and audit semantics for paid resource fulfillment."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.domain.enums import FulfillmentEventType, FulfillmentExecutionState
from app.domain.exceptions import (
    FulfillmentConflictError,
    FulfillmentIntegrityError,
    FulfillmentPermanentError,
    FulfillmentRetryableError,
)
from app.domain.hashing import calculate_quote_hash, sha256_json
from app.domain.json_schema import JSON_SCHEMA_DIALECT
from app.models import FulfillmentExecution
from app.providers.fulfillment import (
    HttpMerchantFulfillmentProvider,
    MerchantFulfillmentInProgressError,
    MerchantFulfillmentPermanentError,
    MerchantFulfillmentRequest,
    MerchantFulfillmentResponseError,
    MerchantFulfillmentResult,
    MerchantFulfillmentRetryableError,
)
from app.services.entitlements import EntitlementApplicationService
from app.services.fulfillments import FulfillmentApplicationService, FulfillmentExpiryFinalizer

INPUT = {"norad_id": 25544}
INPUT_HASH = sha256_json(INPUT)
NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)


@dataclass
class MutableClock:
    current: datetime = NOW

    def __call__(self) -> datetime:
        return self.current


class RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class StaticRepository:
    def __init__(self, value: object) -> None:
        self.value = value

    async def get(self, *_: object) -> object:
        return self.value

    async def get_by_slug(self, *_: object) -> object:
        return self.value

    async def get_by_service_id(self, *_: object) -> object:
        return self.value

    async def get_by_service_id_for_update(self, *_: object) -> object:
        return self.value

    async def get_by_transaction_id(self, *_: object) -> object:
        return self.value


class RecordingExecutionRepository:
    def __init__(self, execution: FulfillmentExecution | None = None) -> None:
        self.execution = execution
        self.events: list[object] = []

    async def get_by_entitlement_id_for_update(
        self,
        _entitlement_id: str,
    ) -> FulfillmentExecution | None:
        return self.execution

    async def get_by_entitlement_id(
        self,
        _entitlement_id: str,
    ) -> FulfillmentExecution | None:
        return self.execution

    async def claim_expired_recoverable_for_update(
        self,
        *,
        now: datetime,
    ) -> FulfillmentExecution | None:
        del now
        return self.execution

    async def create_with_event(
        self,
        execution: FulfillmentExecution,
        *,
        event: object,
    ) -> FulfillmentExecution:
        assert self.execution is None
        self.execution = execution
        self.events.append(event)
        return execution

    async def update_with_event(
        self,
        execution: FulfillmentExecution,
        *,
        event: object,
    ) -> FulfillmentExecution:
        assert execution is self.execution
        assert event.execution_revision == execution.revision + 1  # type: ignore[attr-defined]
        execution.revision += 1
        self.events.append(event)
        return execution


class StubCapabilities:
    def __init__(self, entitlement: SimpleNamespace) -> None:
        self._entitlement = entitlement

    def verify(self, token: str) -> SimpleNamespace:
        assert token == "short-lived-capability"
        return SimpleNamespace(
            entitlement_id=self._entitlement.id,
            account_id=self._entitlement.account_id,
        )

    def require_binding(self, _claims: object, **bindings: object) -> None:
        assert bindings["entitlement_id"] == self._entitlement.id
        assert bindings["input_hash"] == self._entitlement.input_hash


class EligiblePayment:
    def __init__(self) -> None:
        self.reverification_calls: list[str] = []

    async def verify_transaction_for_value_release(self, transaction_id: str) -> object:
        assert transaction_id == "txn_00000000000000000000000000"
        self.reverification_calls.append(transaction_id)
        return SimpleNamespace(transaction_id=transaction_id)

    async def require_local_value_release_eligibility(
        self,
        transaction_id: str,
        *,
        for_update: bool = False,
    ) -> object:
        assert transaction_id == "txn_00000000000000000000000000"
        assert for_update is True
        return SimpleNamespace(id=transaction_id)


class RecordingCompensationService:
    def __init__(self) -> None:
        self.executions: list[FulfillmentExecution] = []

    async def stage_for_permanent_failure(
        self,
        execution: FulfillmentExecution,
        *,
        occurred_at: datetime,
    ) -> object:
        assert occurred_at == NOW
        self.executions.append(execution)
        return SimpleNamespace(id="cmp_00000000000000000000000000")


class SequencedProvider:
    def __init__(self, *outcomes: MerchantFulfillmentResult | Exception) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[MerchantFulfillmentRequest] = []

    async def execute(
        self,
        request: MerchantFulfillmentRequest,
        *,
        endpoint_path: str,
    ) -> MerchantFulfillmentResult:
        assert endpoint_path == "/internal/v1/fulfillments"
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class Harness:
    service: FulfillmentApplicationService
    entitlement: SimpleNamespace
    quote: SimpleNamespace
    catalog_service: SimpleNamespace
    executions: RecordingExecutionRepository
    provider: SequencedProvider
    session: RecordingSession
    clock: MutableClock


def build_harness(
    *outcomes: MerchantFulfillmentResult | Exception,
    maximum_attempts: int = 3,
    maximum_result_bytes: int = 262_144,
    execution: FulfillmentExecution | None = None,
    output_schema: dict[str, Any] | None = None,
    output_content_type: str = "application/json",
) -> Harness:
    clock = MutableClock()
    session = RecordingSession()
    merchant_id = "mrc_00000000000000000000000000"
    service_id = "svc_00000000000000000000000000"
    quote_id = "qte_00000000000000000000000000"
    input_schema = {
        "type": "object",
        "properties": {"norad_id": {"type": "integer", "minimum": 1, "maximum": 999_999_999}},
        "required": ["norad_id"],
        "additionalProperties": False,
    }
    paid_output_schema = output_schema if output_schema is not None else {"type": "object"}
    service_snapshot = {
        "version": "1",
        "json_schema_dialect": JSON_SCHEMA_DIALECT,
        "merchant": {
            "id": merchant_id,
            "slug": "orbitintel",
            "name": "OrbitIntel",
        },
        "service": {
            "id": service_id,
            "slug": "orbital-risk-report",
            "name": "Orbital Risk Report",
            "service_type": "report",
            "input_schema": deepcopy(input_schema),
            "output_schema": deepcopy(paid_output_schema),
            "output_content_type": output_content_type,
        },
        "pricing": {
            "amount": 500,
            "currency": "INR",
            "purchase_type": "one_time",
        },
        "fulfillment": {
            "maximum_seconds": 30,
            "refund_on_failure": True,
        },
    }
    quote_fields = {
        "quote_id": quote_id,
        "merchant_id": merchant_id,
        "service_id": service_id,
        "service_snapshot": service_snapshot,
        "input_value": INPUT,
        "input_hash": INPUT_HASH,
        "amount": 500,
        "currency": "INR",
        "purchase_type": "one_time",
        "maximum_fulfillment_seconds": 30,
        "refund_on_fulfillment_failure": True,
        "issued_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(minutes=5),
    }
    quote = SimpleNamespace(
        id=quote_id,
        merchant_id=merchant_id,
        service_id=service_id,
        service_snapshot=service_snapshot,
        input=INPUT,
        input_hash=INPUT_HASH,
        amount=500,
        currency="INR",
        purchase_type="one_time",
        maximum_fulfillment_seconds=30,
        refund_on_fulfillment_failure=True,
        issued_at=quote_fields["issued_at"],
        expires_at=quote_fields["expires_at"],
        quote_hash=calculate_quote_hash(**quote_fields),
    )
    entitlement = SimpleNamespace(
        id="ent_00000000000000000000000000",
        account_id="acct_00000000000000000000000000",
        transaction_id="txn_00000000000000000000000000",
        merchant_id=merchant_id,
        service_id=service_id,
        quote_id=quote_id,
        quote_hash=quote.quote_hash,
        amount=500,
        currency="INR",
        purchase_type="one_time",
        input_hash=INPUT_HASH,
        expires_at=NOW + timedelta(minutes=10),
    )
    merchant = SimpleNamespace(
        id=entitlement.merchant_id,
        slug="orbitintel",
    )
    catalog_service = SimpleNamespace(
        id=entitlement.service_id,
        slug="orbital-risk-report",
        input_schema=deepcopy(input_schema),
        output_schema=deepcopy(paid_output_schema),
        output_content_type=output_content_type,
    )
    config = SimpleNamespace(
        id="sfc_00000000000000000000000000",
        revision=1,
        provider_type="http",
        enabled=True,
        endpoint_url="http://127.0.0.1:8100/internal/v1/fulfillments",
        request_timeout_seconds=15,
        maximum_attempts=maximum_attempts,
    )
    provider = SequencedProvider(*outcomes)
    executions = RecordingExecutionRepository(execution)
    application = FulfillmentApplicationService(
        session,  # type: ignore[arg-type]
        StubCapabilities(entitlement),  # type: ignore[arg-type]
        provider,
        payment_eligibility=EligiblePayment(),
        provider_base_url="http://127.0.0.1:8100",
        execution_lease=timedelta(seconds=30),
        maximum_result_bytes=maximum_result_bytes,
        clock=clock,
    )
    application._entitlements = StaticRepository(entitlement)  # noqa: SLF001
    application._merchants = StaticRepository(merchant)  # noqa: SLF001
    application._quotes = StaticRepository(quote)  # noqa: SLF001
    application._services = StaticRepository(catalog_service)  # noqa: SLF001
    application._configs = StaticRepository(config)  # noqa: SLF001
    application._compensation_cases = StaticRepository(None)  # noqa: SLF001
    application._compensations = RecordingCompensationService()  # noqa: SLF001
    application._executions = executions  # noqa: SLF001
    return Harness(
        application,
        entitlement,
        quote,
        catalog_service,
        executions,
        provider,
        session,
        clock,
    )


async def execute(harness: Harness) -> Any:
    return await harness.service.execute(
        merchant_slug="orbitintel",
        service_slug="orbital-risk-report",
        input_value=INPUT,
        token="short-lived-capability",
    )


def merchant_request(*, request_timeout_seconds: float = 30.0) -> MerchantFulfillmentRequest:
    return MerchantFulfillmentRequest(
        fulfillment_execution_id="ful_00000000000000000000000000",
        service_id="svc_00000000000000000000000000",
        service_slug="orbital-risk-report",
        input=INPUT,
        input_hash=INPUT_HASH,
        request_timeout_seconds=request_timeout_seconds,
    )


@pytest.fixture(autouse=True)
def trust_test_entitlement_integrity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        EntitlementApplicationService,
        "verify_integrity",
        staticmethod(lambda _entitlement: None),
    )


@pytest.mark.asyncio
async def test_retryable_failure_reuses_execution_and_retry_can_succeed() -> None:
    result = {"data_source": "CelesTrak", "norad_id": 25544}
    harness = build_harness(
        MerchantFulfillmentRetryableError("CELESTRAK_UNAVAILABLE", "temporary"),
        MerchantFulfillmentResult(result_content_type="application/json", result=result),
    )

    with pytest.raises(FulfillmentRetryableError) as caught:
        await execute(harness)
    assert caught.value.reason_code == "FULFILLMENT_RETRYABLE_FAILURE"
    execution_id = harness.executions.execution.id
    assert harness.executions.execution.execution_state is (
        FulfillmentExecutionState.RETRYABLE_FAILURE
    )
    assert harness.executions.execution.compensation_required is False

    operation = await execute(harness)

    assert operation.status_code == 200
    assert operation.result is not None
    assert operation.result.result == result
    assert operation.result.result_hash == sha256_json(result)
    assert harness.executions.execution.id == execution_id
    assert harness.executions.execution.execution_state is FulfillmentExecutionState.SUCCEEDED
    assert harness.executions.execution.attempt_count == 2
    assert {request.fulfillment_execution_id for request in harness.provider.requests} == {
        execution_id
    }
    assert [event.event_type for event in harness.executions.events] == [
        FulfillmentEventType.FULFILLMENT_CLAIMED,
        FulfillmentEventType.FULFILLMENT_STARTED,
        FulfillmentEventType.MERCHANT_REQUEST_SENT,
        FulfillmentEventType.FULFILLMENT_RETRY_SCHEDULED,
        FulfillmentEventType.FULFILLMENT_STARTED,
        FulfillmentEventType.MERCHANT_REQUEST_SENT,
        FulfillmentEventType.FULFILLMENT_SUCCEEDED,
    ]
    assert [event.event_type for event in harness.session.added] == [
        FulfillmentEventType.FULFILLMENT_FAILED,
        FulfillmentEventType.MERCHANT_RESPONSE_RECEIVED,
    ]


@pytest.mark.asyncio
async def test_retry_exhaustion_is_terminal_and_requires_compensation() -> None:
    harness = build_harness(
        MerchantFulfillmentRetryableError("CELESTRAK_UNAVAILABLE", "temporary"),
        MerchantFulfillmentRetryableError("CELESTRAK_UNAVAILABLE", "temporary"),
        maximum_attempts=2,
    )

    with pytest.raises(FulfillmentRetryableError):
        await execute(harness)
    with pytest.raises(FulfillmentPermanentError) as exhausted:
        await execute(harness)
    assert exhausted.value.reason_code == "FULFILLMENT_COMPENSATION_REQUIRED"

    execution = harness.executions.execution
    assert execution.execution_state is FulfillmentExecutionState.PERMANENT_FAILURE
    assert execution.attempt_count == 2
    assert execution.failure_code == "CELESTRAK_UNAVAILABLE"
    assert execution.compensation_required is True
    assert harness.executions.events[-1].event_type is FulfillmentEventType.COMPENSATION_REQUIRED
    assert harness.executions.events[-1].reason_code == "FULFILLMENT_COMPENSATION_REQUIRED"
    assert harness.executions.events[-1].event_metadata == {
        "failure_code": "CELESTRAK_UNAVAILABLE",
        "attempt_count": 2,
    }
    assert [event.event_type for event in harness.session.added] == [
        FulfillmentEventType.FULFILLMENT_FAILED,
        FulfillmentEventType.FULFILLMENT_FAILED,
    ]

    with pytest.raises(FulfillmentPermanentError) as replayed:
        await execute(harness)
    assert replayed.value.reason_code == "ENTITLEMENT_COMPENSATION_QUARANTINED"
    assert len(harness.provider.requests) == 2


@pytest.mark.asyncio
async def test_existing_compensation_case_blocks_before_fulfillment_claim() -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"norad_id": 25_544},
        )
    )
    harness.service._compensation_cases = StaticRepository(  # noqa: SLF001
        SimpleNamespace(id="cmp_00000000000000000000000000")
    )

    with pytest.raises(FulfillmentPermanentError) as blocked:
        await execute(harness)

    assert blocked.value.reason_code == "ENTITLEMENT_COMPENSATION_QUARANTINED"
    assert harness.executions.execution is None
    assert harness.provider.requests == []


@pytest.mark.asyncio
async def test_compensation_case_committed_before_dispatch_blocks_merchant_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"norad_id": 25_544},
        )
    )
    compensation_cases = StaticRepository(None)
    harness.service._compensation_cases = compensation_cases  # noqa: SLF001
    eligibility = harness.service._payment_eligibility  # noqa: SLF001
    original_verify = eligibility.verify_transaction_for_value_release

    async def verify_then_quarantine(transaction_id: str) -> object:
        proof = await original_verify(transaction_id)
        compensation_cases.value = SimpleNamespace(id="cmp_00000000000000000000000000")
        return proof

    monkeypatch.setattr(
        eligibility,
        "verify_transaction_for_value_release",
        verify_then_quarantine,
    )

    with pytest.raises(FulfillmentPermanentError) as blocked:
        await execute(harness)

    assert blocked.value.reason_code == "ENTITLEMENT_COMPENSATION_QUARANTINED"
    assert harness.provider.requests == []
    assert harness.executions.execution is not None
    assert harness.executions.execution.result_json is None


@pytest.mark.asyncio
async def test_compensation_case_committed_during_merchant_call_blocks_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"norad_id": 25_544},
        )
    )
    compensation_cases = StaticRepository(None)
    harness.service._compensation_cases = compensation_cases  # noqa: SLF001
    original_execute = harness.provider.execute

    async def execute_then_quarantine(*args: object, **kwargs: object) -> object:
        result = await original_execute(*args, **kwargs)  # type: ignore[arg-type]
        compensation_cases.value = SimpleNamespace(id="cmp_00000000000000000000000000")
        return result

    monkeypatch.setattr(harness.provider, "execute", execute_then_quarantine)

    with pytest.raises(FulfillmentPermanentError) as blocked:
        await execute(harness)

    assert blocked.value.reason_code == "ENTITLEMENT_COMPENSATION_QUARANTINED"
    assert len(harness.provider.requests) == 1
    assert harness.executions.execution is not None
    assert harness.executions.execution.result_json is None
    assert harness.executions.execution.completed_at is None


@pytest.mark.asyncio
async def test_downstream_in_progress_does_not_burn_logical_attempts() -> None:
    result = {"data_source": "CelesTrak", "norad_id": 25544}
    harness = build_harness(
        MerchantFulfillmentInProgressError(
            "ORBITINTEL_EXECUTION_IN_PROGRESS",
            "still running",
        ),
        MerchantFulfillmentInProgressError(
            "ORBITINTEL_EXECUTION_IN_PROGRESS",
            "still running",
        ),
        MerchantFulfillmentResult(result_content_type="application/json", result=result),
        maximum_attempts=1,
    )

    first = await execute(harness)
    second = await execute(harness)
    completed = await execute(harness)

    assert first.status_code == second.status_code == 202
    assert first.reason_code == second.reason_code == "FULFILLMENT_PROVIDER_IN_PROGRESS"
    assert completed.status_code == 200
    assert completed.result is not None
    assert harness.executions.execution.attempt_count == 1
    assert harness.executions.execution.execution_state is FulfillmentExecutionState.SUCCEEDED
    assert len(harness.provider.requests) == 3


@pytest.mark.asyncio
async def test_permanent_provider_rejection_persists_compensation_evidence() -> None:
    harness = build_harness(
        MerchantFulfillmentPermanentError("NORAD_NOT_FOUND", "permanent"),
    )

    with pytest.raises(FulfillmentPermanentError) as caught:
        await execute(harness)

    assert caught.value.reason_code == "FULFILLMENT_COMPENSATION_REQUIRED"
    execution = harness.executions.execution
    assert execution.execution_state is FulfillmentExecutionState.PERMANENT_FAILURE
    assert execution.failure_code == "NORAD_NOT_FOUND"
    assert execution.compensation_required is True
    assert harness.executions.events[-1].event_type is FulfillmentEventType.COMPENSATION_REQUIRED
    assert harness.session.added[-1].event_type is FulfillmentEventType.FULFILLMENT_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", [None, SimpleNamespace(enabled=False)])
async def test_unavailable_route_is_claimed_and_audited_before_retry(
    configuration: object | None,
) -> None:
    harness = build_harness()
    harness.service._configs = StaticRepository(configuration)  # noqa: SLF001

    with pytest.raises(FulfillmentRetryableError) as caught:
        await execute(harness)

    assert caught.value.reason_code == "FULFILLMENT_RETRYABLE_FAILURE"
    execution = harness.executions.execution
    assert execution is not None
    assert execution.execution_state is FulfillmentExecutionState.RETRYABLE_FAILURE
    assert execution.failure_code == "FULFILLMENT_PROVIDER_UNAVAILABLE"
    assert execution.attempt_count == 1
    assert harness.provider.requests == []
    assert [event.event_type for event in harness.executions.events] == [
        FulfillmentEventType.FULFILLMENT_CLAIMED,
        FulfillmentEventType.FULFILLMENT_STARTED,
        FulfillmentEventType.FULFILLMENT_RETRY_SCHEDULED,
    ]
    assert harness.session.added[-1].event_type is FulfillmentEventType.FULFILLMENT_FAILED


@pytest.mark.asyncio
async def test_invalid_route_configuration_exhausts_with_compensation() -> None:
    harness = build_harness(maximum_attempts=1)
    harness.service._configs = StaticRepository(  # noqa: SLF001
        SimpleNamespace(
            id="sfc_00000000000000000000000000",
            revision=1,
            provider_type="http",
            enabled=True,
            endpoint_url="http://merchant.invalid/internal/v1/fulfillments",
            request_timeout_seconds=15,
            maximum_attempts=1,
        )
    )

    with pytest.raises(FulfillmentPermanentError) as caught:
        await execute(harness)

    assert caught.value.reason_code == "FULFILLMENT_COMPENSATION_REQUIRED"
    execution = harness.executions.execution
    assert execution is not None
    assert execution.execution_state is FulfillmentExecutionState.PERMANENT_FAILURE
    assert execution.failure_code == "FULFILLMENT_PROVIDER_CONFIGURATION_INVALID"
    assert execution.compensation_required is True
    assert harness.provider.requests == []


@pytest.mark.asyncio
async def test_request_deadline_is_bounded_by_quote_and_execution_lease() -> None:
    result = {"data_source": "CelesTrak", "norad_id": 25544}
    harness = build_harness(
        MerchantFulfillmentResult(result_content_type="application/json", result=result)
    )
    harness.service._configs = StaticRepository(  # noqa: SLF001
        SimpleNamespace(
            id="sfc_00000000000000000000000000",
            revision=1,
            provider_type="http",
            enabled=True,
            endpoint_url="http://127.0.0.1:8100/internal/v1/fulfillments",
            request_timeout_seconds=60,
            maximum_attempts=3,
        )
    )

    await execute(harness)

    assert harness.provider.requests[0].request_timeout_seconds == 29.0


@pytest.mark.asyncio
async def test_request_renews_same_generation_lease_after_slow_payment_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {"data_source": "CelesTrak", "norad_id": 25544}
    harness = build_harness(
        MerchantFulfillmentResult(result_content_type="application/json", result=result)
    )
    eligibility = EligiblePayment()

    async def slow_reverification(transaction_id: str) -> object:
        harness.clock.current += timedelta(seconds=31)
        return await EligiblePayment.verify_transaction_for_value_release(
            eligibility,
            transaction_id,
        )

    original_execute = harness.provider.execute

    async def slow_merchant(*args: object, **kwargs: object) -> object:
        provider_result = await original_execute(*args, **kwargs)  # type: ignore[arg-type]
        harness.clock.current += timedelta(seconds=15)
        return provider_result

    monkeypatch.setattr(eligibility, "verify_transaction_for_value_release", slow_reverification)
    monkeypatch.setattr(harness.provider, "execute", slow_merchant)
    harness.service._payment_eligibility = eligibility  # type: ignore[assignment]  # noqa: SLF001

    operation = await execute(harness)

    assert operation.status_code == 200
    assert operation.result is not None
    assert operation.result.completed_at == NOW + timedelta(seconds=46)
    assert harness.provider.requests[0].request_timeout_seconds == 15.0


@pytest.mark.asyncio
async def test_retry_uses_first_request_route_snapshot_after_live_config_edit() -> None:
    result = {"data_source": "CelesTrak", "norad_id": 25544}
    harness = build_harness(
        MerchantFulfillmentRetryableError("CELESTRAK_UNAVAILABLE", "temporary"),
        MerchantFulfillmentResult(result_content_type="application/json", result=result),
    )

    with pytest.raises(FulfillmentRetryableError):
        await execute(harness)
    execution = harness.executions.execution
    assert execution is not None
    assert execution.endpoint_url == "http://127.0.0.1:8100/internal/v1/fulfillments"
    assert execution.fulfillment_config_revision == 1
    config = harness.service._configs.value  # type: ignore[attr-defined]  # noqa: SLF001
    config.endpoint_url = "http://127.0.0.1:8100/internal/v1/alternate"
    config.request_timeout_seconds = 1
    config.maximum_attempts = 1
    config.revision = 2

    completed = await execute(harness)

    assert completed.status_code == 200
    assert len(harness.provider.requests) == 2
    assert harness.provider.requests[1].request_timeout_seconds == 15.0
    assert execution.endpoint_url == "http://127.0.0.1:8100/internal/v1/fulfillments"
    assert execution.fulfillment_config_revision == 1
    assert execution.maximum_attempts == 3


@pytest.mark.asyncio
async def test_disabling_pinned_live_config_stops_redispatch() -> None:
    harness = build_harness(
        MerchantFulfillmentRetryableError("CELESTRAK_UNAVAILABLE", "temporary"),
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"norad_id": 25544},
        ),
    )

    with pytest.raises(FulfillmentRetryableError):
        await execute(harness)
    config = harness.service._configs.value  # type: ignore[attr-defined]  # noqa: SLF001
    config.enabled = False

    with pytest.raises(FulfillmentRetryableError):
        await execute(harness)

    assert len(harness.provider.requests) == 1
    assert harness.executions.execution.failure_code == "FULFILLMENT_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_config_disabled_during_payment_proof_is_rechecked_before_first_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"norad_id": 25544},
        )
    )
    eligibility = EligiblePayment()
    config = harness.service._configs.value  # type: ignore[attr-defined]  # noqa: SLF001

    async def verify_then_disable(transaction_id: str) -> object:
        proof = await EligiblePayment.verify_transaction_for_value_release(
            eligibility,
            transaction_id,
        )
        config.enabled = False
        return proof

    monkeypatch.setattr(eligibility, "verify_transaction_for_value_release", verify_then_disable)
    harness.service._payment_eligibility = eligibility  # type: ignore[assignment]  # noqa: SLF001

    with pytest.raises(FulfillmentRetryableError):
        await execute(harness)

    assert harness.provider.requests == []
    assert harness.executions.execution is not None
    assert harness.executions.execution.endpoint_url is None
    assert harness.executions.execution.failure_code == "FULFILLMENT_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_outcome", "maximum_result_bytes", "failure_code"),
    [
        (
            MerchantFulfillmentResponseError("MERCHANT_RESPONSE_INVALID", "invalid"),
            262_144,
            "MERCHANT_RESPONSE_INVALID",
        ),
        (
            MerchantFulfillmentResult(
                result_content_type="application/json",
                result={"oversized": "x" * 2_000},
            ),
            1_024,
            "FULFILLMENT_RESULT_TOO_LARGE",
        ),
    ],
)
async def test_invalid_or_oversized_result_fails_closed_for_reconciliation(
    provider_outcome: MerchantFulfillmentResult | Exception,
    maximum_result_bytes: int,
    failure_code: str,
) -> None:
    harness = build_harness(
        provider_outcome,
        maximum_result_bytes=maximum_result_bytes,
    )

    with pytest.raises(FulfillmentIntegrityError) as caught:
        await execute(harness)

    assert caught.value.reason_code == "FULFILLMENT_PROVIDER_RESPONSE_INVALID"
    execution = harness.executions.execution
    assert execution.execution_state is FulfillmentExecutionState.RECONCILIATION_REQUIRED
    assert execution.failure_code == failure_code
    assert execution.compensation_required is False
    assert execution.result_json is None
    assert harness.executions.events[-1].event_type is FulfillmentEventType.FULFILLMENT_FAILED
    assert harness.executions.events[-1].reason_code == "FULFILLMENT_RECONCILIATION_REQUIRED"

    with pytest.raises(FulfillmentIntegrityError) as replayed:
        await execute(harness)
    assert replayed.value.reason_code == "FULFILLMENT_RECONCILIATION_REQUIRED"
    assert len(harness.provider.requests) == 1


@pytest.mark.asyncio
async def test_result_content_type_mismatch_fails_closed_without_result() -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/problem+json",
            result={"status": "operational"},
        ),
    )

    with pytest.raises(FulfillmentIntegrityError) as caught:
        await execute(harness)

    assert caught.value.reason_code == "FULFILLMENT_PROVIDER_RESPONSE_INVALID"
    execution = harness.executions.execution
    assert execution.execution_state is FulfillmentExecutionState.RECONCILIATION_REQUIRED
    assert execution.failure_code == "FULFILLMENT_RESULT_CONTENT_TYPE_MISMATCH"
    assert execution.result_content_type is None
    assert execution.result_json is None
    assert execution.result_hash is None
    assert execution.result_size_bytes is None


@pytest.mark.asyncio
async def test_schema_invalid_result_fails_closed_without_result() -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"status": 7},
        ),
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
    )

    with pytest.raises(FulfillmentIntegrityError) as caught:
        await execute(harness)

    assert caught.value.reason_code == "FULFILLMENT_PROVIDER_RESPONSE_INVALID"
    execution = harness.executions.execution
    assert execution.execution_state is FulfillmentExecutionState.RECONCILIATION_REQUIRED
    assert execution.failure_code == "FULFILLMENT_RESULT_SCHEMA_INVALID"
    assert execution.result_content_type is None
    assert execution.result_json is None
    assert execution.result_hash is None
    assert execution.result_size_bytes is None


@pytest.mark.asyncio
async def test_invalid_output_schema_configuration_fails_closed_without_result() -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"status": "operational"},
        ),
        output_schema={"type": "not-a-json-schema-type"},
    )

    with pytest.raises(FulfillmentIntegrityError) as caught:
        await execute(harness)

    assert caught.value.reason_code == "FULFILLMENT_PROVIDER_RESPONSE_INVALID"
    execution = harness.executions.execution
    assert execution.execution_state is FulfillmentExecutionState.RECONCILIATION_REQUIRED
    assert execution.failure_code == "FULFILLMENT_SERVICE_OUTPUT_SCHEMA_INVALID"
    assert execution.result_json is None


@pytest.mark.asyncio
async def test_contract_valid_result_is_persisted_as_success() -> None:
    result = {"status": "operational"}
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result=result,
        ),
        output_schema={
            "type": "object",
            "properties": {"status": {"const": "operational"}},
            "required": ["status"],
            "additionalProperties": False,
        },
    )

    operation = await execute(harness)

    assert operation.status_code == 200
    assert operation.result is not None
    assert operation.result.result == result
    assert operation.result.result_content_type == "application/json"
    assert operation.result.result_hash == sha256_json(result)
    execution = harness.executions.execution
    assert execution.execution_state is FulfillmentExecutionState.SUCCEEDED
    assert execution.result_json == result
    assert execution.failure_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "output_schema"),
    [
        (["available", 25_544], {"type": "array"}),
        ("available", {"type": "string"}),
        (25_544, {"type": "integer"}),
        (True, {"type": "boolean"}),
        (None, {"type": "null"}),
    ],
)
async def test_general_json_root_results_are_persisted_and_replayed(
    result: Any,
    output_schema: dict[str, Any],
) -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result=result,
        ),
        output_schema=output_schema,
    )

    first = await execute(harness)
    replay = await execute(harness)

    assert first.result is not None
    assert replay.result is not None
    assert first.result.result == result
    assert replay.result.result == result
    assert first.result.result_hash == sha256_json(result)
    assert first.result.replayed_result is False
    assert replay.result.replayed_result is True
    assert len(harness.provider.requests) == 1


@pytest.mark.asyncio
async def test_later_service_schema_edits_do_not_change_the_paid_quote_contract() -> None:
    result = {"status": "operational"}
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result=result,
        ),
        output_schema={
            "type": "object",
            "properties": {"status": {"const": "operational"}},
            "required": ["status"],
            "additionalProperties": False,
        },
    )
    harness.catalog_service.input_schema = {
        "type": "object",
        "properties": {"replacement": {"type": "string"}},
        "required": ["replacement"],
        "additionalProperties": False,
    }
    harness.catalog_service.output_schema = {
        "type": "object",
        "properties": {"replacement": {"type": "boolean"}},
        "required": ["replacement"],
        "additionalProperties": False,
    }
    harness.catalog_service.output_content_type = "application/problem+json"
    harness.catalog_service.slug = "renamed-after-payment"

    operation = await execute(harness)

    assert operation.status_code == 200
    assert operation.result is not None
    assert operation.result.result == result
    assert harness.provider.requests[0].input == INPUT
    assert harness.provider.requests[0].service_slug == "orbital-risk-report"
    assert harness.executions.execution.execution_state is FulfillmentExecutionState.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["tampered", "mismatched"])
async def test_tampered_or_mismatched_paid_quote_fails_closed(
    failure_kind: str,
) -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"status": "operational"},
        ),
    )
    if failure_kind == "tampered":
        harness.quote.service_snapshot["service"]["output_schema"] = {"type": "array"}
    else:
        harness.quote.id = "qte_00000000000000000000000001"
        harness.quote.quote_hash = calculate_quote_hash(
            quote_id=harness.quote.id,
            merchant_id=harness.quote.merchant_id,
            service_id=harness.quote.service_id,
            service_snapshot=harness.quote.service_snapshot,
            input_value=harness.quote.input,
            input_hash=harness.quote.input_hash,
            amount=harness.quote.amount,
            currency=harness.quote.currency,
            purchase_type=harness.quote.purchase_type,
            maximum_fulfillment_seconds=harness.quote.maximum_fulfillment_seconds,
            refund_on_fulfillment_failure=harness.quote.refund_on_fulfillment_failure,
            issued_at=harness.quote.issued_at,
            expires_at=harness.quote.expires_at,
        )

    with pytest.raises(FulfillmentIntegrityError) as caught:
        await execute(harness)

    assert caught.value.reason_code == "FULFILLMENT_INTEGRITY_FAILED"
    assert harness.executions.execution is None
    assert harness.provider.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "error_type", "reason_code"),
    [
        (
            503,
            {"reason_code": "ORBITINTEL_FAULT_RETRYABLE"},
            MerchantFulfillmentRetryableError,
            "ORBITINTEL_FAULT_RETRYABLE",
        ),
        (
            422,
            {"reason_code": "ORBITINTEL_FAULT_PERMANENT"},
            MerchantFulfillmentPermanentError,
            "ORBITINTEL_FAULT_PERMANENT",
        ),
        (
            200,
            {
                "fulfillment_execution_id": "ful_00000000000000000000000001",
                "service_id": "svc_00000000000000000000000000",
                "input_hash": INPUT_HASH,
                "result_content_type": "application/json",
                "result": {"norad_id": 25544},
            },
            MerchantFulfillmentResponseError,
            "FULFILLMENT_PROVIDER_RESPONSE_INVALID",
        ),
    ],
)
async def test_http_adapter_classifies_retryable_permanent_and_invalid_responses(
    status_code: int,
    body: dict[str, object],
    error_type: type[Exception],
    reason_code: str,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer " + "s" * 32
        return httpx.Response(status_code, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = HttpMerchantFulfillmentProvider(
            client,
            base_url="http://127.0.0.1:8100",
            shared_secret="s" * 32,
            maximum_response_bytes=4_096,
        )
        with pytest.raises(error_type) as caught:
            await provider.execute(
                merchant_request(),
                endpoint_path="/internal/v1/fulfillments",
            )

    assert caught.value.reason_code == reason_code  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason_code", "error_type"),
    [
        ("ORBITINTEL_EXECUTION_IN_PROGRESS", MerchantFulfillmentInProgressError),
        ("ORBITINTEL_EXECUTION_UNCERTAIN", MerchantFulfillmentResponseError),
        ("ORBITINTEL_IDEMPOTENCY_MISMATCH", MerchantFulfillmentResponseError),
    ],
)
async def test_http_adapter_classifies_orbitintel_conflicts_without_false_retry(
    reason_code: str,
    error_type: type[Exception],
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(409, json={"reason_code": reason_code})
        )
    ) as client:
        provider = HttpMerchantFulfillmentProvider(
            client,
            base_url="http://127.0.0.1:8100",
            shared_secret="s" * 32,
            maximum_response_bytes=4_096,
        )
        with pytest.raises(error_type) as caught:
            await provider.execute(
                merchant_request(),
                endpoint_path="/internal/v1/fulfillments",
            )

    assert caught.value.reason_code == reason_code  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding", "replacement"),
    [
        ("service_id", "svc_00000000000000000000000001"),
        ("input_hash", "sha256:" + "f" * 64),
    ],
)
async def test_http_adapter_rejects_mismatched_response_bindings(
    binding: str,
    replacement: object,
) -> None:
    request = merchant_request()
    body: dict[str, Any] = {
        "fulfillment_execution_id": request.fulfillment_execution_id,
        "service_id": request.service_id,
        "input_hash": request.input_hash,
        "result_content_type": "application/json",
        "result": {"norad_id": 25544},
    }
    body[binding] = replacement

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    ) as client:
        provider = HttpMerchantFulfillmentProvider(
            client,
            base_url="http://127.0.0.1:8100",
            shared_secret="s" * 32,
            maximum_response_bytes=4_096,
        )
        with pytest.raises(MerchantFulfillmentResponseError) as caught:
            await provider.execute(
                request,
                endpoint_path="/internal/v1/fulfillments",
            )

    assert caught.value.reason_code == "FULFILLMENT_PROVIDER_RESPONSE_INVALID"


@pytest.mark.asyncio
async def test_http_adapter_accepts_exact_response_bindings() -> None:
    request = merchant_request()
    body = {
        "fulfillment_execution_id": request.fulfillment_execution_id,
        "service_id": request.service_id,
        "input_hash": request.input_hash,
        "result_content_type": "application/json",
        "result": {"status": "generic-contract-valid"},
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    ) as client:
        provider = HttpMerchantFulfillmentProvider(
            client,
            base_url="http://127.0.0.1:8100",
            shared_secret="s" * 32,
            maximum_response_bytes=4_096,
        )
        result = await provider.execute(
            request,
            endpoint_path="/internal/v1/fulfillments",
        )

    assert result.result == {"status": "generic-contract-valid"}


@pytest.mark.asyncio
async def test_http_adapter_maps_transport_timeout_to_retryable_failure() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        provider = HttpMerchantFulfillmentProvider(
            client,
            base_url="http://127.0.0.1:8100",
            shared_secret="s" * 32,
            maximum_response_bytes=4_096,
        )
        with pytest.raises(MerchantFulfillmentRetryableError) as caught:
            await provider.execute(
                merchant_request(),
                endpoint_path="/internal/v1/fulfillments",
            )

    assert caught.value.reason_code == "FULFILLMENT_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_http_adapter_hard_deadline_bounds_the_entire_merchant_operation() -> None:
    async def respond(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = HttpMerchantFulfillmentProvider(
            client,
            base_url="http://127.0.0.1:8100",
            shared_secret="s" * 32,
            maximum_response_bytes=4_096,
        )
        with pytest.raises(MerchantFulfillmentRetryableError) as caught:
            await provider.execute(
                merchant_request(request_timeout_seconds=0.01),
                endpoint_path="/internal/v1/fulfillments",
            )

    assert caught.value.reason_code == "FULFILLMENT_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_http_adapter_keeps_non_json_5xx_retryable() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"temporarily unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = HttpMerchantFulfillmentProvider(
            client,
            base_url="http://127.0.0.1:8100",
            shared_secret="s" * 32,
            maximum_response_bytes=4_096,
        )
        with pytest.raises(MerchantFulfillmentRetryableError) as caught:
            await provider.execute(
                merchant_request(),
                endpoint_path="/internal/v1/fulfillments",
            )

    assert caught.value.reason_code == "FULFILLMENT_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_http_adapter_rejects_encoded_responses_before_decoding() -> None:
    class EncodedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"not-decoded"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            stream=EncodedStream(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = HttpMerchantFulfillmentProvider(
            client,
            base_url="http://127.0.0.1:8100",
            shared_secret="s" * 32,
            maximum_response_bytes=4_096,
        )
        with pytest.raises(MerchantFulfillmentResponseError) as caught:
            await provider.execute(
                merchant_request(),
                endpoint_path="/internal/v1/fulfillments",
            )

    assert caught.value.reason_code == "FULFILLMENT_PROVIDER_RESPONSE_INVALID"


def test_failure_code_sanitization_matches_postgresql_ascii_constraint() -> None:
    assert FulfillmentApplicationService._safe_failure_code("CELESTRAK_5XX") == (  # noqa: SLF001
        "CELESTRAK_5XX"
    )
    assert FulfillmentApplicationService._safe_failure_code("Å_FAILURE") == (  # noqa: SLF001
        "FULFILLMENT_PROVIDER_FAILURE"
    )


@pytest.mark.asyncio
async def test_expired_execution_lease_is_audited_and_fenced_before_retry() -> None:
    execution = FulfillmentExecution(
        id="ful_00000000000000000000000000",
        entitlement_id="ent_00000000000000000000000000",
        account_id="acct_00000000000000000000000000",
        transaction_id="txn_00000000000000000000000000",
        merchant_id="mrc_00000000000000000000000000",
        service_id="svc_00000000000000000000000000",
        input_hash=INPUT_HASH,
        execution_state=FulfillmentExecutionState.EXECUTING,
        attempt_count=1,
        started_at=NOW - timedelta(minutes=2),
        completed_at=None,
        failed_at=None,
        result_content_type=None,
        result_json=None,
        result_hash=None,
        result_size_bytes=None,
        failure_code=None,
        compensation_required=False,
        revision=3,
        lease_generation=1,
        lease_expires_at=NOW - timedelta(minutes=1),
    )
    harness = build_harness(execution=execution)

    claimed, owns_lease = await harness.service._claim_execution(  # noqa: SLF001
        harness.entitlement,
        maximum_attempts=3,
    )

    assert owns_lease is True
    assert claimed is execution
    assert execution.execution_state is FulfillmentExecutionState.EXECUTING
    assert execution.attempt_count == 2
    assert execution.lease_generation == 2
    assert execution.failure_code is None
    assert execution.lease_expires_at == NOW + timedelta(seconds=30)
    assert [event.event_type for event in harness.executions.events] == [
        FulfillmentEventType.FULFILLMENT_RETRY_SCHEDULED,
        FulfillmentEventType.FULFILLMENT_STARTED,
    ]
    assert harness.session.added[-1].event_type is FulfillmentEventType.FULFILLMENT_FAILED
    assert harness.executions.events[0].reason_code == "FULFILLMENT_EXECUTION_LEASE_EXPIRED"
    assert harness.executions.events[0].event_metadata == {"attempt_count": 1}


@pytest.mark.asyncio
async def test_expiry_finalizer_closes_retryable_execution_with_compensation() -> None:
    execution = FulfillmentExecution(
        id="ful_00000000000000000000000000",
        entitlement_id="ent_00000000000000000000000000",
        account_id="acct_00000000000000000000000000",
        transaction_id="txn_00000000000000000000000000",
        merchant_id="mrc_00000000000000000000000000",
        service_id="svc_00000000000000000000000000",
        input_hash=INPUT_HASH,
        execution_state=FulfillmentExecutionState.RETRYABLE_FAILURE,
        attempt_count=1,
        started_at=NOW - timedelta(minutes=2),
        completed_at=None,
        failed_at=NOW - timedelta(minutes=1),
        result_content_type=None,
        result_json=None,
        result_hash=None,
        result_size_bytes=None,
        failure_code="CELESTRAK_UNAVAILABLE",
        compensation_required=False,
        revision=4,
        lease_generation=1,
        lease_expires_at=None,
    )
    harness = build_harness(execution=execution)
    finalizer = FulfillmentExpiryFinalizer(  # type: ignore[arg-type]
        harness.session,
        clock=harness.clock,
    )
    finalizer._executions = harness.executions  # noqa: SLF001
    finalizer._compensations = RecordingCompensationService()  # noqa: SLF001

    finalized = await finalizer.finalize_one()

    assert finalized is execution
    assert execution.execution_state is FulfillmentExecutionState.PERMANENT_FAILURE
    assert execution.failure_code == "FULFILLMENT_ENTITLEMENT_EXPIRED"
    assert execution.compensation_required is True
    assert execution.failed_at == NOW
    assert harness.session.added[-1].event_type is FulfillmentEventType.FULFILLMENT_FAILED
    assert harness.executions.events[-1].event_type is FulfillmentEventType.COMPENSATION_REQUIRED


@pytest.mark.asyncio
async def test_stale_owner_cannot_send_after_generation_or_state_changes() -> None:
    result = {"data_source": "CelesTrak", "norad_id": 25544}
    harness = build_harness(
        MerchantFulfillmentResult(result_content_type="application/json", result=result)
    )
    execution, owns_lease = await harness.service._claim_execution(  # noqa: SLF001
        harness.entitlement,
        maximum_attempts=3,
    )
    assert owns_lease is True
    stale_generation = execution.lease_generation
    execution.lease_generation += 1

    with pytest.raises(FulfillmentConflictError) as generation_lost:
        await harness.service._record_request_sent(  # noqa: SLF001
            execution,
            entitlement=harness.entitlement,
            quote=harness.quote,
            expected_generation=stale_generation,
        )
    assert generation_lost.value.reason_code == "FULFILLMENT_ALREADY_CLAIMED"
    assert harness.provider.requests == []

    execution.lease_generation = stale_generation
    execution.execution_state = FulfillmentExecutionState.SUCCEEDED
    with pytest.raises(FulfillmentConflictError) as state_lost:
        await harness.service._record_request_sent(  # noqa: SLF001
            execution,
            entitlement=harness.entitlement,
            quote=harness.quote,
            expected_generation=stale_generation,
        )
    assert state_lost.value.reason_code == "FULFILLMENT_ALREADY_CLAIMED"
    assert harness.provider.requests == []


@pytest.mark.asyncio
async def test_late_owner_cannot_persist_success_after_lease_expiry() -> None:
    harness = build_harness()
    execution, owns_lease = await harness.service._claim_execution(  # noqa: SLF001
        harness.entitlement,
        maximum_attempts=3,
    )
    assert owns_lease is True
    harness.clock.current = NOW + timedelta(seconds=31)

    with pytest.raises(FulfillmentConflictError) as caught:
        await harness.service._record_success(  # noqa: SLF001
            harness.entitlement,
            execution_id=execution.id,
            expected_generation=execution.lease_generation,
            result={"norad_id": 25544},
            result_content_type="application/json",
            result_bytes=b'{"norad_id":25544}',
        )

    assert caught.value.reason_code == "FULFILLMENT_ALREADY_CLAIMED"
    assert execution.execution_state is FulfillmentExecutionState.EXECUTING
    assert execution.result_json is None
