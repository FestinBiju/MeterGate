"""Post-entitlement payment anomalies must still fence later value release."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain.exceptions import EntitlementConflictError, FulfillmentRetryableError
from app.providers.fulfillment import MerchantFulfillmentResult, MerchantFulfillmentRetryableError
from app.services.entitlements import EntitlementApplicationService
from tests.test_fulfillment_failures import build_harness, execute

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        raise AssertionError("the capability happy path must not roll back")


class StaticRepository:
    def __init__(self, value: object | None) -> None:
        self.value = value

    async def get(self, *_: object) -> object | None:
        return self.value

    async def get_by_entitlement_id(self, *_: object) -> object | None:
        return self.value


class MutablePaymentEligibility:
    def __init__(self) -> None:
        self.unresolved = False
        self.provider_unresolved = False
        self.calls: list[tuple[str, bool]] = []
        self.reverification_calls: list[str] = []

    async def verify_transaction_for_value_release(self, transaction_id: str) -> object:
        self.reverification_calls.append(transaction_id)
        if self.unresolved or self.provider_unresolved:
            raise EntitlementConflictError(
                "Payment reconciliation evidence must be explicitly resolved before value release",
                "ENTITLEMENT_RECONCILIATION_REQUIRED",
            )
        return SimpleNamespace(transaction_id=transaction_id)

    async def require_local_value_release_eligibility(
        self,
        transaction_id: str,
        *,
        for_update: bool = False,
    ) -> object:
        self.calls.append((transaction_id, for_update))
        if self.unresolved:
            raise EntitlementConflictError(
                "Payment reconciliation evidence must be explicitly resolved before value release",
                "ENTITLEMENT_RECONCILIATION_REQUIRED",
            )
        return SimpleNamespace(id=transaction_id)


class RecordingCapabilityIssuer:
    def __init__(self) -> None:
        self.calls = 0

    def issue(self, entitlement: object) -> object:
        self.calls += 1
        return SimpleNamespace(
            token=f"capability-{self.calls}",
            claims=SimpleNamespace(
                jti=f"cap_0000000000000000000000000{self.calls}",
                expires_at=NOW + timedelta(minutes=5),
                maximum_executions=1,
            ),
        )


@pytest.mark.asyncio
async def test_later_payment_anomaly_blocks_capability_until_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RecordingSession()
    eligibility = MutablePaymentEligibility()
    capabilities = RecordingCapabilityIssuer()
    entitlement = SimpleNamespace(
        id="ent_00000000000000000000000000",
        account_id="acct_00000000000000000000000000",
        transaction_id="txn_00000000000000000000000000",
        merchant_id="mrc_00000000000000000000000000",
        service_id="svc_00000000000000000000000000",
        expires_at=NOW + timedelta(minutes=10),
    )
    merchant = SimpleNamespace(id=entitlement.merchant_id)
    service = SimpleNamespace(id=entitlement.service_id, merchant_id=merchant.id)
    application = EntitlementApplicationService(
        session,  # type: ignore[arg-type]
        eligibility,  # type: ignore[arg-type]
        capabilities,  # type: ignore[arg-type]
        entitlement_ttl=timedelta(minutes=10),
        clock=lambda: NOW,
    )
    application._entitlements = StaticRepository(entitlement)  # type: ignore[assignment]  # noqa: SLF001
    application._merchants = StaticRepository(merchant)  # type: ignore[assignment]  # noqa: SLF001
    application._services = StaticRepository(service)  # type: ignore[assignment]  # noqa: SLF001
    application._executions = StaticRepository(None)  # type: ignore[assignment]  # noqa: SLF001
    monkeypatch.setattr(
        EntitlementApplicationService,
        "verify_integrity",
        staticmethod(lambda _: None),
    )

    first = await application.issue_capability(
        entitlement.id,
        account_id=entitlement.account_id,
    )
    eligibility.unresolved = True

    with pytest.raises(EntitlementConflictError) as blocked:
        await application.issue_capability(
            entitlement.id,
            account_id=entitlement.account_id,
        )

    assert blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"
    assert capabilities.calls == 1

    eligibility.unresolved = False
    restored = await application.issue_capability(
        entitlement.id,
        account_id=entitlement.account_id,
    )

    assert first.token == "capability-1"
    assert restored.token == "capability-2"
    assert eligibility.calls == [(entitlement.transaction_id, True)] * 3
    assert capabilities.calls == 2
    assert session.commits == 2


@pytest.mark.asyncio
async def test_later_payment_anomaly_blocks_merchant_execution_until_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"norad_id": 25_544, "status": "available"},
        )
    )
    eligibility = MutablePaymentEligibility()
    eligibility.unresolved = True
    harness.service._payment_eligibility = eligibility  # type: ignore[assignment]  # noqa: SLF001
    monkeypatch.setattr(
        EntitlementApplicationService,
        "verify_integrity",
        staticmethod(lambda _: None),
    )

    with pytest.raises(EntitlementConflictError) as blocked:
        await execute(harness)

    assert blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"
    assert harness.executions.execution is None
    assert harness.provider.requests == []

    eligibility.unresolved = False
    operation = await execute(harness)

    assert operation.status_code == 200
    assert operation.reason_code == "FULFILLMENT_SUCCEEDED"
    assert len(harness.provider.requests) == 1
    assert eligibility.calls == [(harness.entitlement.transaction_id, True)] * 4
    assert eligibility.reverification_calls == [harness.entitlement.transaction_id]


@pytest.mark.asyncio
async def test_payment_anomaly_committed_during_merchant_call_blocks_result_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"norad_id": 25_544, "status": "available"},
        )
    )
    eligibility = MutablePaymentEligibility()
    harness.service._payment_eligibility = eligibility  # type: ignore[assignment]  # noqa: SLF001
    original_execute = harness.provider.execute

    async def execute_then_observe_anomaly(*args: object, **kwargs: object) -> object:
        result = await original_execute(*args, **kwargs)  # type: ignore[arg-type]
        eligibility.unresolved = True
        return result

    monkeypatch.setattr(harness.provider, "execute", execute_then_observe_anomaly)
    monkeypatch.setattr(
        EntitlementApplicationService,
        "verify_integrity",
        staticmethod(lambda _: None),
    )

    with pytest.raises(EntitlementConflictError) as blocked:
        await execute(harness)

    assert blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"
    assert len(harness.provider.requests) == 1
    assert harness.executions.execution is not None
    assert harness.executions.execution.result_json is None
    assert harness.executions.execution.completed_at is None
    assert eligibility.calls == [(harness.entitlement.transaction_id, True)] * 3
    assert eligibility.reverification_calls == [harness.entitlement.transaction_id]


@pytest.mark.asyncio
async def test_each_retry_owner_gets_fresh_provider_proof_after_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(
        MerchantFulfillmentRetryableError("CELESTRAK_UNAVAILABLE", "temporary"),
        MerchantFulfillmentResult(
            result_content_type="application/json",
            result={"norad_id": 25_544, "status": "available"},
        ),
    )
    eligibility = MutablePaymentEligibility()
    harness.service._payment_eligibility = eligibility  # type: ignore[assignment]  # noqa: SLF001
    monkeypatch.setattr(
        EntitlementApplicationService,
        "verify_integrity",
        staticmethod(lambda _: None),
    )

    with pytest.raises(FulfillmentRetryableError):
        await execute(harness)
    eligibility.provider_unresolved = True

    with pytest.raises(EntitlementConflictError) as blocked:
        await execute(harness)

    assert blocked.value.reason_code == "ENTITLEMENT_RECONCILIATION_REQUIRED"
    assert len(harness.provider.requests) == 1
    assert harness.executions.execution is not None
    assert harness.executions.execution.failure_code == (
        "FULFILLMENT_PAYMENT_REVERIFICATION_BLOCKED"
    )

    eligibility.provider_unresolved = False
    completed = await execute(harness)

    assert completed.status_code == 200
    assert len(harness.provider.requests) == 2
    assert eligibility.reverification_calls == [harness.entitlement.transaction_id] * 3
