from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.api.v1.dependencies import (
    require_current_account,
    require_operator,
    require_recent_operator,
)
from app.api.v1.operator import _worker_projection, reconcile_payment
from app.cache.operator import OperatorRateLimitExceeded, RedisOperatorRateLimiter
from app.core.config import Settings
from app.db.session import get_session
from app.domain.enums import PaymentRefundState
from app.domain.exceptions import AuthenticationForbiddenError
from app.models import Incident, OperatorAction
from app.schemas.operator import OperatorDecisionRequest, WorkItem
from app.services.operator import OperatorService

ACCOUNT_ID = "acct_00000000000000000000000001"


class ScalarSession:
    def __init__(self, value: object | None) -> None:
        self.value = value

    async def scalar(self, _statement: object) -> object | None:
        return self.value


class SequenceScalarSession:
    def __init__(self, values: list[object | None]) -> None:
        self.values = values

    async def scalar(self, _statement: object) -> object | None:
        return self.values.pop(0)


class ScriptRedis:
    def __init__(self, outcomes: list[list[int]]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple] = []

    async def eval(self, *args: object) -> list[int]:
        self.calls.append(args)
        return self.outcomes.pop(0)


class AllowingLimiter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def require(self, **values: object) -> None:
        self.calls.append(values)


class OperatorPaymentService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def reconcile_transaction_for_operator(
        self, transaction_id: str, *, operator_account_id: str
    ) -> object:
        self.calls.append((transaction_id, operator_account_id))
        return SimpleNamespace(
            response=SimpleNamespace(state="payment_pending", revision=7),
            status_code=200,
        )


class AsyncScope:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class IncidentSession:
    def __init__(
        self, *, incident: Incident | None = None, underlying: object | None = None
    ) -> None:
        self.incident = incident
        self.underlying = underlying
        self.added: list[object] = []
        self.incident_key: str | None = None
        self.commits = 0

    async def scalar(self, _statement: object) -> object | None:
        if self.incident is not None:
            return self.incident
        return self.incident_key

    def begin_nested(self) -> AsyncScope:
        return AsyncScope()

    def add(self, value: object) -> None:
        self.added.append(value)
        if isinstance(value, Incident):
            self.incident_key = value.id

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def get(self, _model: object, _identifier: str) -> object | None:
        return self.underlying


@pytest.mark.asyncio
async def test_active_operator_is_allowed_and_missing_or_disabled_role_is_denied() -> None:
    current = SimpleNamespace(account=SimpleNamespace(id=ACCOUNT_ID))
    active = SimpleNamespace(account_id=ACCOUNT_ID, role="operator", status="active")
    context = await require_operator(current, ScalarSession(active))
    assert context.account_id == ACCOUNT_ID
    assert context.role == "operator"

    with pytest.raises(AuthenticationForbiddenError, match="active operator role"):
        await require_operator(current, ScalarSession(None))


def test_operator_api_denies_normal_buyer_and_allows_active_operator(
    make_client: Any,
) -> None:
    current = SimpleNamespace(account=SimpleNamespace(id=ACCOUNT_ID))

    normal_client: TestClient = make_client()
    normal_client.app.dependency_overrides[require_current_account] = lambda: current
    normal_client.app.dependency_overrides[get_session] = lambda: ScalarSession(None)
    denied = normal_client.get("/api/v1/operator/session")
    assert denied.status_code == 403
    assert denied.json()["reason_code"] == "OPERATOR_ROLE_REQUIRED"

    operator_client: TestClient = make_client()
    operator_client.app.dependency_overrides[require_current_account] = lambda: current
    assignment = SimpleNamespace(account_id=ACCOUNT_ID, role="operator", status="active")
    operator_client.app.dependency_overrides[get_session] = lambda: ScalarSession(assignment)
    allowed = operator_client.get("/api/v1/operator/session")
    assert allowed.status_code == 200
    assert allowed.json() == {"account_id": ACCOUNT_ID, "role": "operator"}


def test_operator_api_exposes_only_evidence_backed_mutations(make_client: Any) -> None:
    paths = make_client().get("/openapi.json").json()["paths"]
    mutation_paths = {
        path
        for path, methods in paths.items()
        if path.startswith("/api/v1/operator/") and "post" in methods
    }
    assert mutation_paths == {
        "/api/v1/operator/compensations/{case_id}/approve",
        "/api/v1/operator/compensations/{case_id}/reject",
        "/api/v1/operator/refunds/{refund_id}/reconcile",
        "/api/v1/operator/payments/{transaction_id}/reconcile",
        "/api/v1/operator/fulfillments/{fulfillment_id}/reconcile",
        "/api/v1/operator/incidents/{incident_id}/acknowledge",
        "/api/v1/operator/incidents/{incident_id}/resolve",
    }


@pytest.mark.asyncio
async def test_recent_operator_rejects_stale_passkey_and_accepts_recent_passkey() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/metergate",
        redis_url="redis://localhost:6379/15",
        operator_reauth_max_age_seconds=300,
    )
    active = SimpleNamespace(account_id=ACCOUNT_ID, role="operator", status="active")
    now = datetime.now(UTC)
    stale = SimpleNamespace(
        account=SimpleNamespace(id=ACCOUNT_ID),
        state=SimpleNamespace(authenticated_at=now - timedelta(seconds=301)),
    )
    with pytest.raises(AuthenticationForbiddenError, match="Recent operator passkey"):
        await require_recent_operator(stale, ScalarSession(active), settings)

    recent = SimpleNamespace(
        account=SimpleNamespace(id=ACCOUNT_ID),
        state=SimpleNamespace(authenticated_at=now - timedelta(seconds=30)),
    )
    context = await require_recent_operator(recent, ScalarSession(active), settings)
    assert context.account_id == ACCOUNT_ID


@pytest.mark.asyncio
async def test_redis_operator_rate_limit_is_per_account_and_fails_at_boundary() -> None:
    redis = ScriptRedis([[1, 60], [2, 59]])
    limiter = RedisOperatorRateLimiter(redis)  # type: ignore[arg-type]
    await limiter.require(
        account_id=ACCOUNT_ID,
        action="refund-reconcile",
        limit=1,
        window_seconds=60,
    )
    with pytest.raises(OperatorRateLimitExceeded) as raised:
        await limiter.require(
            account_id=ACCOUNT_ID,
            action="refund-reconcile",
            limit=1,
            window_seconds=60,
        )
    assert raised.value.retry_after_seconds == 59
    assert "metergate:operator-rate:v1" in redis.calls[0][2]
    assert ACCOUNT_ID in redis.calls[0][2]


@pytest.mark.asyncio
async def test_operator_payment_reconciliation_records_returned_response_state() -> None:
    transaction_id = ACCOUNT_ID.replace("acct_", "txn_")
    response = Response()
    application_service = OperatorPaymentService()
    session = IncidentSession(underlying=SimpleNamespace(revision=7))
    limiter = AllowingLimiter()
    settings = Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/metergate",
        redis_url="redis://localhost:6379/15",
    )

    result = await reconcile_payment(
        transaction_id,
        response,
        application_service,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        SimpleNamespace(account_id=ACCOUNT_ID),
        limiter,  # type: ignore[arg-type]
        settings,
    )

    assert result.state == "payment_pending"
    assert application_service.calls == [(transaction_id, ACCOUNT_ID)]
    assert limiter.calls[0]["action"] == "payment-reconcile"
    actions = [value for value in session.added if isinstance(value, OperatorAction)]
    assert len(actions) == 1
    assert actions[0].evidence == {"resulting_state": "payment_pending"}
    assert actions[0].idempotency_key.endswith(":7")


def test_compensation_request_rejects_client_amount_override() -> None:
    with pytest.raises(ValueError):
        OperatorDecisionRequest.model_validate(
            {
                "reason_code": "OPERATOR_APPROVE_CONFIRMED_NON_DELIVERY",
                "amount": 1,
            }
        )


@pytest.mark.asyncio
async def test_worker_projection_requires_fresh_heartbeats_for_all_three_workers() -> None:
    now = datetime.now(UTC)
    fresh = SimpleNamespace(
        last_heartbeat=now - timedelta(seconds=5),
        last_successful_work=now - timedelta(seconds=6),
        backlog_count=1,
    )
    stale = SimpleNamespace(
        last_heartbeat=now - timedelta(seconds=31),
        last_successful_work=None,
        backlog_count=2,
    )
    rows = await _worker_projection(SequenceScalarSession([fresh, fresh, stale]), 30)
    assert [row["worker_type"] for row in rows] == [
        "razorpay_webhook",
        "entitlement",
        "refund",
    ]
    assert [row["status"] for row in rows] == ["healthy", "healthy", "stale"]
    assert rows[2]["alert_code"] == "WORKER_HEARTBEAT_STALE"


@pytest.mark.asyncio
async def test_missing_worker_heartbeat_is_stale_not_healthy() -> None:
    rows = await _worker_projection(SequenceScalarSession([None, None, None]), 30)
    assert all(row["status"] == "stale" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item_type", "resource_id", "transaction_id", "refund_id", "fulfillment_id"),
    [
        (
            "payment_reconciliation",
            ACCOUNT_ID.replace("acct_", "txn_"),
            ACCOUNT_ID.replace("acct_", "txn_"),
            None,
            None,
        ),
        (
            "refund_reconciliation",
            ACCOUNT_ID.replace("acct_", "rfd_"),
            ACCOUNT_ID.replace("acct_", "txn_"),
            ACCOUNT_ID.replace("acct_", "rfd_"),
            None,
        ),
        (
            "fulfillment_reconciliation",
            ACCOUNT_ID.replace("acct_", "ful_"),
            ACCOUNT_ID.replace("acct_", "txn_"),
            None,
            ACCOUNT_ID.replace("acct_", "ful_"),
        ),
        ("entitlement_outbox_stuck", "outbox-record-1", None, None, None),
    ],
)
async def test_incident_detection_is_idempotent_and_binds_domain_evidence(
    item_type: str,
    resource_id: str,
    transaction_id: str | None,
    refund_id: str | None,
    fulfillment_id: str | None,
) -> None:
    session = IncidentSession()
    item = WorkItem(
        type=item_type,
        severity="critical",
        state="reconciliation_required",
        resource_id=resource_id,
        transaction_id=transaction_id,
        age_seconds=120,
        reason_code="TEST_ANOMALY_REASON",
    )
    service = OperatorService(session)  # type: ignore[arg-type]

    await service._ensure_incidents([item, item])

    incidents = [value for value in session.added if isinstance(value, Incident)]
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.transaction_id == transaction_id
    assert incident.refund_id == refund_id
    assert incident.fulfillment_id == fulfillment_id
    assert incident.evidence["source_id"] == resource_id
    assert incident.evidence["reason_code"] == "TEST_ANOMALY_REASON"
    assert incident.idempotency_key == f"incident:{item_type}:{resource_id}"


@pytest.mark.asyncio
async def test_incident_acknowledgement_does_not_resolve_and_resolution_fails_closed() -> None:
    transaction_id = ACCOUNT_ID.replace("acct_", "txn_")
    refund_id = ACCOUNT_ID.replace("acct_", "rfd_")
    operator_id = ACCOUNT_ID
    incident = Incident(
        id=ACCOUNT_ID.replace("acct_", "inc_"),
        incident_type="refund_reconciliation",
        severity="critical",
        transaction_id=transaction_id,
        refund_id=refund_id,
        fulfillment_id=None,
        compensation_id=None,
        state="open",
        summary_code="REFUND_RECONCILIATION_REQUIRED",
        evidence={"source_id": refund_id, "reason_code": "REFUND_RECONCILIATION_REQUIRED"},
        idempotency_key=f"incident:refund_reconciliation:{refund_id}",
    )
    unresolved_refund = SimpleNamespace(refund_state=PaymentRefundState.REFUND_UNCERTAIN)
    session = IncidentSession(incident=incident, underlying=unresolved_refund)
    service = OperatorService(session)  # type: ignore[arg-type]
    recorded: list[str] = []

    async def record_action(**values: object) -> None:
        recorded.append(str(values["reason_code"]))

    service.record_action = record_action  # type: ignore[method-assign]
    acknowledged = await service.acknowledge_incident(incident.id, operator_id)
    assert acknowledged.state == "acknowledged"
    assert acknowledged.resolved_at is None
    assert acknowledged.assigned_operator_id == operator_id
    assert recorded == ["INCIDENT_ACKNOWLEDGED"]

    with pytest.raises(ValueError, match="INCIDENT_UNDERLYING_STATE_UNRESOLVED"):
        await service.resolve_incident(incident.id, operator_id)

    unresolved_refund.refund_state = PaymentRefundState.REFUNDED
    resolved = await service.resolve_incident(incident.id, operator_id)
    assert resolved.state == "resolved"
    assert resolved.resolved_at is not None
    assert recorded[-1] == "INCIDENT_RESOLVED"
