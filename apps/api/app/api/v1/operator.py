"""Authenticated, server-authorized operator control plane."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, text

from app.api.v1.dependencies import (
    FulfillmentApplicationDependency,
    OperatorDependency,
    OperatorRateLimiterDependency,
    PaymentApplicationDependency,
    RecentOperatorDependency,
    RefundApplicationDependency,
    SessionDependency,
    SettingsDependency,
)
from app.cache.operator import OperatorRateLimitExceeded
from app.models import (
    CommerceOutboxEvent,
    CompensationCase,
    CompensationEvent,
    Entitlement,
    FulfillmentEvent,
    FulfillmentExecution,
    Incident,
    OperatorAction,
    OperatorDecision,
    PaymentTransaction,
    PaymentTransactionEvent,
    PolicyEvaluation,
    PurchaseAuthorization,
    Quote,
    RazorpayWebhookEvent,
    RefundOutboxEvent,
    WorkerHeartbeat,
)
from app.schemas.operator import (
    IncidentResponse,
    OperatorDecisionRequest,
    OperatorDecisionResponse,
    SystemHealthResponse,
    WorkItem,
)
from app.services.compensations import payment_refund_response
from app.services.operator import OperatorService
from app.services.worker_health import WORKER_TYPES

router = APIRouter(prefix="/operator", tags=["operator"])


async def _require_rate_limit(
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
    *,
    account_id: str,
    action: str,
    reconciliation: bool = False,
) -> None:
    try:
        await limiter.require(
            account_id=account_id,
            action=action,
            limit=(
                settings.operator_reconciliation_rate_limit
                if reconciliation
                else settings.operator_mutation_rate_limit
            ),
            window_seconds=settings.operator_rate_limit_window_seconds,
        )
    except OperatorRateLimitExceeded as error:
        raise HTTPException(
            429,
            detail={"code": "OPERATOR_RATE_LIMITED"},
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error
    except Exception as error:
        raise HTTPException(503, detail={"code": "OPERATOR_RATE_LIMIT_UNAVAILABLE"}) from error


def _conflict(error: ValueError) -> HTTPException:
    return HTTPException(409, detail={"code": str(error)})


@router.get("/session")
async def operator_session(operator: OperatorDependency) -> dict[str, str]:
    return {"account_id": operator.account_id, "role": operator.role}


@router.get("/transactions")
async def transactions(
    session: SessionDependency,
    operator: OperatorDependency,
    limit: int = 50,
) -> list[dict[str, Any]]:
    del operator
    records = (
        await session.scalars(
            select(PaymentTransaction)
            .order_by(PaymentTransaction.created_at.desc())
            .limit(max(1, min(limit, 100)))
        )
    ).all()
    return [
        {
            "transaction_id": record.id,
            "account_id": record.account_id,
            "merchant_id": record.merchant_id,
            "service_id": record.service_id,
            "amount": record.amount,
            "currency": record.currency,
            "payment_state": str(record.transaction_state),
            "created_at": record.created_at,
        }
        for record in records
    ]


@router.get("/work-items", response_model=list[WorkItem])
async def work_items(
    session: SessionDependency,
    operator: OperatorDependency,
    settings: SettingsDependency,
) -> list[WorkItem]:
    del operator
    return await OperatorService(
        session,
        outbox_stuck_seconds=settings.outbox_stuck_seconds,
        refund_uncertain_alert_seconds=settings.refund_uncertain_alert_seconds,
    ).work_items()


async def _worker_projection(session: SessionDependency, stale_seconds: int) -> list[dict]:
    now = datetime.now(UTC)
    result: list[dict] = []
    for worker_type in WORKER_TYPES:
        heartbeat = await session.scalar(
            select(WorkerHeartbeat)
            .where(WorkerHeartbeat.worker_type == worker_type)
            .order_by(WorkerHeartbeat.last_heartbeat.desc())
            .limit(1)
        )
        stale = (
            heartbeat is None
            or (now - heartbeat.last_heartbeat.astimezone(UTC)).total_seconds() > stale_seconds
        )
        result.append(
            {
                "worker_type": worker_type,
                "status": "stale" if stale else "healthy",
                "last_heartbeat": heartbeat.last_heartbeat if heartbeat else None,
                "last_successful_work": heartbeat.last_successful_work if heartbeat else None,
                "backlog_count": heartbeat.backlog_count if heartbeat else 0,
                "alert_code": "WORKER_HEARTBEAT_STALE" if stale else None,
            }
        )
    return result


@router.get("/alerts", response_model=list[WorkItem])
async def alerts(
    session: SessionDependency,
    operator: OperatorDependency,
    settings: SettingsDependency,
) -> list[WorkItem]:
    del operator
    items = [
        item
        for item in await OperatorService(
            session,
            outbox_stuck_seconds=settings.outbox_stuck_seconds,
            refund_uncertain_alert_seconds=settings.refund_uncertain_alert_seconds,
        ).work_items()
        if item.severity in {"high", "critical"}
    ]
    for worker in await _worker_projection(session, settings.worker_heartbeat_stale_seconds):
        if worker["status"] == "stale":
            items.append(
                WorkItem(
                    type=f"{worker['worker_type']}_worker_stale",
                    severity="critical",
                    state="stale",
                    resource_id=worker["worker_type"],
                    age_seconds=0,
                    reason_code="WORKER_HEARTBEAT_STALE",
                )
            )
    recent_webhooks = (
        await session.scalars(
            select(RazorpayWebhookEvent)
            .order_by(RazorpayWebhookEvent.received_at.desc())
            .limit(100)
        )
    ).all()
    delayed = next(
        (
            event
            for event in recent_webhooks
            if (event.received_at - event.provider_created_at).total_seconds()
            > settings.webhook_lag_alert_seconds
        ),
        None,
    )
    if delayed is not None:
        lag = int((delayed.received_at - delayed.provider_created_at).total_seconds())
        items.append(
            WorkItem(
                type="provider_webhook_lag",
                severity="high",
                state="delayed",
                resource_id=delayed.id,
                transaction_id=delayed.transaction_id,
                age_seconds=max(0, lag),
                reason_code="PROVIDER_WEBHOOK_LAG_HIGH",
            )
        )
    return items


@router.get("/compensations/{case_id}/review")
async def compensation_review(
    case_id: str, session: SessionDependency, operator: OperatorDependency
) -> dict:
    del operator
    try:
        return await OperatorService(session).compensation_review(case_id)
    except ValueError as error:
        raise HTTPException(404, detail={"code": str(error)}) from error


async def _decide(
    *,
    case_id: str,
    body: OperatorDecisionRequest,
    action: str,
    session: SessionDependency,
    operator: RecentOperatorDependency,
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
) -> OperatorDecisionResponse:
    await _require_rate_limit(
        limiter, settings, account_id=operator.account_id, action=f"compensation-{action}"
    )
    try:
        decision = await OperatorService(session).decide_compensation(
            case_id, operator.account_id, action, body.reason_code, body.note
        )
    except ValueError as error:
        raise _conflict(error) from error
    case = await session.get(CompensationCase, case_id)
    return OperatorDecisionResponse(
        id=decision.id,
        compensation_case_id=case_id,
        action=action,
        reason_code=decision.reason_code,
        decided_at=decision.decided_at,
        decision_hash=decision.decision_hash,
        approved_refund_amount=case.approved_refund_amount,
    )


@router.post("/compensations/{case_id}/approve", response_model=OperatorDecisionResponse)
async def approve(
    case_id: str,
    body: OperatorDecisionRequest,
    session: SessionDependency,
    operator: RecentOperatorDependency,
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
) -> OperatorDecisionResponse:
    return await _decide(
        case_id=case_id,
        body=body,
        action="approve",
        session=session,
        operator=operator,
        limiter=limiter,
        settings=settings,
    )


@router.post("/compensations/{case_id}/reject", response_model=OperatorDecisionResponse)
async def reject(
    case_id: str,
    body: OperatorDecisionRequest,
    session: SessionDependency,
    operator: RecentOperatorDependency,
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
) -> OperatorDecisionResponse:
    return await _decide(
        case_id=case_id,
        body=body,
        action="reject",
        session=session,
        operator=operator,
        limiter=limiter,
        settings=settings,
    )


@router.post("/refunds/{refund_id}/reconcile")
async def reconcile_refund(
    refund_id: str,
    response: Response,
    application_service: RefundApplicationDependency,
    session: SessionDependency,
    operator: RecentOperatorDependency,
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
):
    await _require_rate_limit(
        limiter,
        settings,
        account_id=operator.account_id,
        action="refund-reconcile",
        reconciliation=True,
    )
    refund = await application_service.reconcile_refund(refund_id)
    await OperatorService(session).record_action(
        operator_id=operator.account_id,
        action="refund_reconciliation_triggered",
        resource_type="refund",
        resource_id=refund_id,
        transaction_id=refund.transaction_id,
        reason_code="OPERATOR_RECONCILIATION_TRIGGERED",
        evidence={"resulting_state": str(refund.refund_state)},
        idempotency_key=f"operator-refund-reconcile:{refund_id}:{refund.revision}",
    )
    response.headers["Cache-Control"] = "private, no-store"
    return payment_refund_response(refund)


@router.post("/payments/{transaction_id}/reconcile")
async def reconcile_payment(
    transaction_id: str,
    response: Response,
    application_service: PaymentApplicationDependency,
    session: SessionDependency,
    operator: RecentOperatorDependency,
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
):
    await _require_rate_limit(
        limiter,
        settings,
        account_id=operator.account_id,
        action="payment-reconcile",
        reconciliation=True,
    )
    result = await application_service.reconcile_transaction_for_operator(
        transaction_id, operator_account_id=operator.account_id
    )
    transaction = await session.get(PaymentTransaction, transaction_id)
    if transaction is None:
        raise HTTPException(
            409,
            detail={"code": "PAYMENT_TRANSACTION_EVIDENCE_UNAVAILABLE"},
        )
    await OperatorService(session).record_action(
        operator_id=operator.account_id,
        action="payment_reconciliation_triggered",
        resource_type="payment_transaction",
        resource_id=transaction_id,
        transaction_id=transaction_id,
        reason_code="OPERATOR_RECONCILIATION_TRIGGERED",
        evidence={"resulting_state": str(result.response.state)},
        idempotency_key=f"operator-payment-reconcile:{transaction_id}:{transaction.revision}",
    )
    response.status_code = result.status_code
    response.headers["Cache-Control"] = "private, no-store"
    return result.response


@router.post("/fulfillments/{fulfillment_id}/reconcile")
async def reconcile_fulfillment(
    fulfillment_id: str,
    response: Response,
    application_service: FulfillmentApplicationDependency,
    session: SessionDependency,
    operator: RecentOperatorDependency,
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
):
    await _require_rate_limit(
        limiter,
        settings,
        account_id=operator.account_id,
        action="fulfillment-reconcile",
        reconciliation=True,
    )
    result = await application_service.reconcile_execution(fulfillment_id)
    execution = await session.get(FulfillmentExecution, fulfillment_id)
    await OperatorService(session).record_action(
        operator_id=operator.account_id,
        action="fulfillment_reconciliation_triggered",
        resource_type="fulfillment",
        resource_id=fulfillment_id,
        transaction_id=execution.transaction_id if execution else None,
        reason_code="OPERATOR_RECONCILIATION_TRIGGERED",
        evidence={"resulting_reason_code": result.reason_code},
        idempotency_key=(
            f"operator-fulfillment-reconcile:{fulfillment_id}:"
            f"{execution.revision if execution is not None else result.reason_code}"
        ),
    )
    response.status_code = result.status_code
    response.headers["Cache-Control"] = "private, no-store"
    return {
        "execution_id": result.execution_id,
        "entitlement_id": result.entitlement_id,
        "reason_code": result.reason_code,
        "result": result.result,
    }


@router.get("/incidents", response_model=list[IncidentResponse])
async def incidents(session: SessionDependency, operator: OperatorDependency) -> list[Incident]:
    del operator
    return list((await session.scalars(select(Incident).order_by(Incident.opened_at.desc()))).all())


async def _mutate_incident(
    *,
    incident_id: str,
    action: str,
    session: SessionDependency,
    operator: RecentOperatorDependency,
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
) -> Incident:
    await _require_rate_limit(
        limiter, settings, account_id=operator.account_id, action=f"incident-{action}"
    )
    service = OperatorService(session)
    try:
        if action == "acknowledge":
            return await service.acknowledge_incident(incident_id, operator.account_id)
        return await service.resolve_incident(incident_id, operator.account_id)
    except ValueError as error:
        raise _conflict(error) from error


@router.post("/incidents/{incident_id}/acknowledge", response_model=IncidentResponse)
async def acknowledge(
    incident_id: str,
    session: SessionDependency,
    operator: RecentOperatorDependency,
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
) -> Incident:
    return await _mutate_incident(
        incident_id=incident_id,
        action="acknowledge",
        session=session,
        operator=operator,
        limiter=limiter,
        settings=settings,
    )


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentResponse)
async def resolve(
    incident_id: str,
    session: SessionDependency,
    operator: RecentOperatorDependency,
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
) -> Incident:
    return await _mutate_incident(
        incident_id=incident_id,
        action="resolve",
        session=session,
        operator=operator,
        limiter=limiter,
        settings=settings,
    )


@router.get("/transactions/{transaction_id}/detail")
async def detail(
    transaction_id: str, session: SessionDependency, operator: OperatorDependency
) -> dict:
    del operator
    try:
        return await OperatorService(session).case_detail(transaction_id)
    except ValueError as error:
        raise HTTPException(404, detail={"code": str(error)}) from error


@router.get("/transactions/{transaction_id}/timeline")
async def timeline(
    transaction_id: str, session: SessionDependency, operator: OperatorDependency
) -> list[dict]:
    del operator
    rows: list[dict] = []
    transaction = await session.get(PaymentTransaction, transaction_id)
    if transaction is None:
        raise HTTPException(404, detail={"code": "PAYMENT_TRANSACTION_NOT_FOUND"})
    quote = await session.get(Quote, transaction.quote_id)
    evaluation = await session.get(PolicyEvaluation, transaction.evaluation_id)
    authorization = await session.get(PurchaseAuthorization, transaction.authorization_id)
    entitlement = await session.scalar(
        select(Entitlement).where(Entitlement.transaction_id == transaction_id)
    )
    for event_type, evidence, occurred_at in (
        ("quote_created", quote, quote.issued_at if quote else None),
        ("policy_evaluated", evaluation, evaluation.created_at if evaluation else None),
        ("authorized", authorization, authorization.authorized_at if authorization else None),
        ("entitlement_issued", entitlement, entitlement.issued_at if entitlement else None),
    ):
        if evidence is not None and occurred_at is not None:
            rows.append(
                {
                    "kind": "commerce",
                    "event_type": event_type,
                    "reason_code": None,
                    "occurred_at": occurred_at,
                    "actor_type": "system",
                }
            )
    for model, kind in (
        (PaymentTransactionEvent, "payment"),
        (FulfillmentEvent, "fulfillment"),
        (CompensationEvent, "compensation"),
    ):
        events = (
            await session.scalars(select(model).where(model.transaction_id == transaction_id))
        ).all()
        rows.extend(
            {
                "kind": kind,
                "event_type": str(event.event_type),
                "reason_code": event.reason_code,
                "occurred_at": event.occurred_at,
                "actor_type": str(event.actor_type),
            }
            for event in events
        )
    case = await session.scalar(
        select(CompensationCase).where(CompensationCase.transaction_id == transaction_id)
    )
    if case is not None:
        decisions = (
            await session.scalars(
                select(OperatorDecision).where(OperatorDecision.compensation_case_id == case.id)
            )
        ).all()
        rows.extend(
            {
                "kind": "operator",
                "event_type": "operator_decision",
                "reason_code": decision.reason_code,
                "occurred_at": decision.decided_at,
                "actor_type": "operator",
            }
            for decision in decisions
        )
    for action in (
        await session.scalars(
            select(OperatorAction).where(OperatorAction.transaction_id == transaction_id)
        )
    ).all():
        rows.append(
            {
                "kind": "operator",
                "event_type": action.action,
                "reason_code": action.reason_code,
                "occurred_at": action.acted_at,
                "actor_type": "operator",
            }
        )
    for incident in (
        await session.scalars(select(Incident).where(Incident.transaction_id == transaction_id))
    ).all():
        rows.append(
            {
                "kind": "incident",
                "event_type": "incident_opened",
                "reason_code": incident.summary_code,
                "occurred_at": incident.opened_at,
                "actor_type": "system",
            }
        )
        if incident.acknowledged_at:
            rows.append(
                {
                    "kind": "incident",
                    "event_type": "incident_acknowledged",
                    "reason_code": "INCIDENT_ACKNOWLEDGED",
                    "occurred_at": incident.acknowledged_at,
                    "actor_type": "operator",
                }
            )
        if incident.resolved_at:
            rows.append(
                {
                    "kind": "incident",
                    "event_type": "incident_resolved",
                    "reason_code": "INCIDENT_RESOLVED",
                    "occurred_at": incident.resolved_at,
                    "actor_type": "operator",
                }
            )
    return sorted(rows, key=lambda row: (row["occurred_at"], row["kind"], row["event_type"]))


@router.get("/system-health", response_model=SystemHealthResponse)
async def system_health(
    request: Request,
    session: SessionDependency,
    operator: OperatorDependency,
    settings: SettingsDependency,
) -> SystemHealthResponse:
    del operator
    await session.execute(text("SELECT 1"))
    redis_status = "unavailable"
    try:
        await request.app.state.redis_client.ping()
        redis_status = "available"
    except Exception:
        pass
    workers = await _worker_projection(session, settings.worker_heartbeat_stale_seconds)
    service = OperatorService(session, outbox_stuck_seconds=settings.outbox_stuck_seconds)
    stuck = await service.work_items()
    return SystemHealthResponse(
        postgresql="available",
        redis=redis_status,
        workers=workers,
        queues={
            "entitlement_pending": int(
                await session.scalar(
                    select(func.count())
                    .select_from(CommerceOutboxEvent)
                    .where(CommerceOutboxEvent.processed_at.is_(None))
                )
                or 0
            ),
            "refund_pending": int(
                await session.scalar(
                    select(func.count())
                    .select_from(RefundOutboxEvent)
                    .where(RefundOutboxEvent.processed_at.is_(None))
                )
                or 0
            ),
            "entitlement_stuck": sum(i.type == "entitlement_outbox_stuck" for i in stuck),
            "refund_stuck": sum(i.type == "refund_outbox_stuck" for i in stuck),
        },
    )


@router.get("/metrics/summary")
async def metrics_summary(session: SessionDependency, operator: OperatorDependency) -> dict:
    del operator
    return await OperatorService(session).metrics_summary()


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(session: SessionDependency, operator: OperatorDependency) -> str:
    del operator
    return await OperatorService(session).metrics()
