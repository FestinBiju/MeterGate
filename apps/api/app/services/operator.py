"""Bounded operator projections, incidents, decisions, and action evidence."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    CompensationDecisionProvenance,
    CompensationDecisionState,
    FulfillmentExecutionState,
    PaymentRefundState,
    PaymentTransactionState,
    RefundOutboxEventType,
)
from app.domain.hashing import canonical_utc_datetime, sha256_json
from app.domain.ids import (
    new_incident_id,
    new_operator_action_id,
    new_operator_decision_id,
    new_refund_outbox_event_id,
)
from app.models import (
    CommerceOutboxEvent,
    CompensationCase,
    FulfillmentExecution,
    Incident,
    McpToolAuditEvent,
    Merchant,
    OperatorAction,
    OperatorDecision,
    PaymentAttempt,
    PaymentRefund,
    PaymentTransaction,
    PaymentTransactionEvent,
    PolicyEvaluation,
    Quote,
    RefundOutboxEvent,
    Service,
)
from app.schemas.operator import WorkItem

APPROVE_REASON = "OPERATOR_APPROVE_CONFIRMED_NON_DELIVERY"
REJECT_REASONS = {
    "OPERATOR_REJECT_VALUE_ALREADY_DELIVERED",
    "OPERATOR_REJECT_DUPLICATE_REQUEST",
}


class OperatorService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        outbox_stuck_seconds: int = 300,
        refund_uncertain_alert_seconds: int = 60,
    ) -> None:
        self.session = session
        self.outbox_stuck_seconds = outbox_stuck_seconds
        self.refund_uncertain_alert_seconds = refund_uncertain_alert_seconds

    async def decide_compensation(
        self,
        case_id: str,
        operator_id: str,
        action: str,
        reason: str,
        note: dict | None,
    ) -> OperatorDecision:
        case = await self.session.scalar(
            select(CompensationCase).where(CompensationCase.id == case_id).with_for_update()
        )
        if case is None:
            raise ValueError("COMPENSATION_NOT_FOUND")
        if (
            CompensationDecisionState(case.decision_state)
            is not CompensationDecisionState.MANUAL_REVIEW
        ):
            raise ValueError("COMPENSATION_NOT_MANUAL_REVIEW")
        if (action == "approve" and reason != APPROVE_REASON) or (
            action == "reject" and reason not in REJECT_REASONS
        ):
            raise ValueError("OPERATOR_REASON_NOT_ALLOWED")
        now = datetime.now(UTC)
        decision_id = new_operator_decision_id()
        payload = {
            "version": "1",
            "id": decision_id,
            "operator_account_id": operator_id,
            "compensation_case_id": case.id,
            "action": action,
            "reason_code": reason,
            "structured_note": note,
            "decided_at": canonical_utc_datetime(now),
            "server_derived_amount": case.amount_paid if action == "approve" else None,
        }
        decision = OperatorDecision(
            id=decision_id,
            operator_account_id=operator_id,
            compensation_case_id=case.id,
            action=action,
            reason_code=reason,
            structured_note=note,
            decided_at=now,
            decision_hash=sha256_json(payload),
        )
        self.session.add(decision)
        case.decision_state = (
            CompensationDecisionState.APPROVED
            if action == "approve"
            else CompensationDecisionState.REJECTED
        )
        case.decision_provenance = (
            CompensationDecisionProvenance.MANUAL_APPROVED
            if action == "approve"
            else CompensationDecisionProvenance.MANUAL_REJECTED
        )
        case.approved_refund_amount = case.amount_paid if action == "approve" else None
        case.decision_reason_code = reason
        if case.decided_at is None:
            case.decided_at = now
        case.closed_at = None if action == "approve" else now
        case.revision += 1
        if action == "approve":
            self.session.add(
                RefundOutboxEvent(
                    id=new_refund_outbox_event_id(),
                    event_type=RefundOutboxEventType.REFUND_REQUESTED,
                    compensation_case_id=case.id,
                    deduplication_key=f"refund:{case.id}",
                    payload_version="1",
                    payload={
                        "compensation_case_id": case.id,
                        "transaction_id": case.transaction_id,
                        "approved_refund_amount": case.amount_paid,
                        "currency": case.currency,
                    },
                    created_at=now,
                    available_at=now,
                    attempt_count=0,
                    lease_generation=0,
                )
            )
        await self.session.commit()
        return decision

    async def record_action(
        self,
        *,
        operator_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        transaction_id: str | None,
        reason_code: str,
        evidence: dict,
        idempotency_key: str,
    ) -> OperatorAction:
        existing = await self.session.scalar(
            select(OperatorAction).where(OperatorAction.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        action_id = new_operator_action_id()
        payload = {
            "version": "1",
            "id": action_id,
            "operator_account_id": operator_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "transaction_id": transaction_id,
            "reason_code": reason_code,
            "evidence": evidence,
            "acted_at": canonical_utc_datetime(now),
        }
        record = OperatorAction(
            id=action_id,
            operator_account_id=operator_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            transaction_id=transaction_id,
            reason_code=reason_code,
            evidence=evidence,
            acted_at=now,
            action_hash=sha256_json(payload),
            idempotency_key=idempotency_key,
        )
        self.session.add(record)
        await self.session.commit()
        return record

    async def work_items(self) -> list[WorkItem]:
        now = datetime.now(UTC)
        items: list[WorkItem] = []
        cases = (
            await self.session.scalars(
                select(CompensationCase).where(
                    CompensationCase.decision_state == CompensationDecisionState.MANUAL_REVIEW
                )
            )
        ).all()
        for case in cases:
            items.append(
                WorkItem(
                    type="manual_compensation_review",
                    severity="high",
                    state="open",
                    resource_id=case.id,
                    transaction_id=case.transaction_id,
                    account_id=case.account_id,
                    merchant_id=case.merchant_id,
                    service_id=case.service_id,
                    age_seconds=self._age(now, case.created_at),
                    reason_code=case.decision_reason_code,
                )
            )
        refunds = (
            await self.session.scalars(
                select(PaymentRefund).where(
                    PaymentRefund.refund_state.in_(
                        [
                            PaymentRefundState.REFUND_UNCERTAIN,
                            PaymentRefundState.RECONCILIATION_REQUIRED,
                        ]
                    )
                )
            )
        ).all()
        for refund in refunds:
            age = self._age(now, refund.created_at)
            if (
                PaymentRefundState(refund.refund_state) is PaymentRefundState.REFUND_UNCERTAIN
                and age < self.refund_uncertain_alert_seconds
            ):
                continue
            items.append(
                WorkItem(
                    type=(
                        "persistent_refund_uncertainty"
                        if PaymentRefundState(refund.refund_state)
                        is PaymentRefundState.REFUND_UNCERTAIN
                        else "refund_reconciliation"
                    ),
                    severity=(
                        "critical"
                        if PaymentRefundState(refund.refund_state)
                        is PaymentRefundState.RECONCILIATION_REQUIRED
                        else "high"
                    ),
                    state=str(refund.refund_state),
                    resource_id=refund.id,
                    transaction_id=refund.transaction_id,
                    age_seconds=age,
                    reason_code=refund.reconciliation_reason_code or "REFUND_UNCERTAIN_PERSISTENT",
                )
            )
        anomaly_codes = {
            "PAYMENT_MULTIPLE_CAPTURES_DETECTED": "multiple_captured_payment_anomaly",
            "PAYMENT_PROVIDER_RESPONSE_MISMATCH": "provider_response_mismatch",
            "PAYMENT_TRANSACTION_INTEGRITY_FAILED": "invalid_value_release_integrity",
            "ENTITLEMENT_PAYMENT_INTEGRITY_FAILED": "invalid_value_release_integrity",
        }
        anomaly_events = (
            await self.session.scalars(
                select(PaymentTransactionEvent).where(
                    PaymentTransactionEvent.reason_code.in_(anomaly_codes)
                )
            )
        ).all()
        for event in anomaly_events:
            transaction = await self.session.get(PaymentTransaction, event.transaction_id)
            items.append(
                WorkItem(
                    type=anomaly_codes[event.reason_code],
                    severity="critical",
                    state="open",
                    resource_id=event.id,
                    transaction_id=event.transaction_id,
                    account_id=transaction.account_id if transaction else None,
                    merchant_id=transaction.merchant_id if transaction else None,
                    service_id=transaction.service_id if transaction else None,
                    age_seconds=self._age(now, event.occurred_at),
                    reason_code=event.reason_code,
                )
            )
        for transaction in (
            await self.session.scalars(
                select(PaymentTransaction).where(
                    PaymentTransaction.transaction_state
                    == PaymentTransactionState.RECONCILIATION_REQUIRED
                )
            )
        ).all():
            items.append(
                WorkItem(
                    type="payment_reconciliation",
                    severity="critical",
                    state="reconciliation_required",
                    resource_id=transaction.id,
                    transaction_id=transaction.id,
                    account_id=transaction.account_id,
                    merchant_id=transaction.merchant_id,
                    service_id=transaction.service_id,
                    age_seconds=self._age(now, transaction.updated_at),
                    reason_code="PAYMENT_RECONCILIATION_REQUIRED",
                )
            )
        for execution in (
            await self.session.scalars(
                select(FulfillmentExecution).where(
                    FulfillmentExecution.execution_state
                    == FulfillmentExecutionState.RECONCILIATION_REQUIRED
                )
            )
        ).all():
            items.append(
                WorkItem(
                    type="fulfillment_reconciliation",
                    severity="critical",
                    state="reconciliation_required",
                    resource_id=execution.id,
                    transaction_id=execution.transaction_id,
                    account_id=execution.account_id,
                    merchant_id=execution.merchant_id,
                    service_id=execution.service_id,
                    age_seconds=self._age(now, execution.updated_at),
                    reason_code=execution.failure_code,
                )
            )
        for model, item_type in (
            (CommerceOutboxEvent, "entitlement_outbox_stuck"),
            (RefundOutboxEvent, "refund_outbox_stuck"),
        ):
            events = (
                await self.session.scalars(select(model).where(model.processed_at.is_(None)))
            ).all()
            for event in events:
                age = self._age(now, event.created_at)
                if age < self.outbox_stuck_seconds:
                    continue
                items.append(
                    WorkItem(
                        type=item_type,
                        severity="high",
                        state="stuck",
                        resource_id=event.id,
                        age_seconds=age,
                        reason_code=event.last_error_code or "OUTBOX_ITEM_STUCK",
                    )
                )
        await self._ensure_incidents(items)
        return sorted(
            items,
            key=lambda item: (
                {"critical": 0, "high": 1, "warning": 2, "info": 3}[item.severity],
                -item.age_seconds,
                item.resource_id,
            ),
        )

    async def _ensure_incidents(self, items: list[WorkItem]) -> None:
        created = False
        for item in items:
            if item.type == "manual_compensation_review":
                continue
            key = f"incident:{item.type}:{item.resource_id}"
            exists = await self.session.scalar(
                select(Incident.id).where(Incident.idempotency_key == key)
            )
            if exists is not None:
                continue
            try:
                async with self.session.begin_nested():
                    self.session.add(
                        Incident(
                            id=new_incident_id(),
                            incident_type=item.type,
                            severity=item.severity,
                            transaction_id=item.transaction_id,
                            refund_id=(
                                item.resource_id
                                if item.type
                                in {"refund_reconciliation", "persistent_refund_uncertainty"}
                                else None
                            ),
                            fulfillment_id=(
                                item.resource_id
                                if item.type == "fulfillment_reconciliation"
                                else None
                            ),
                            compensation_id=None,
                            state="open",
                            opened_at=datetime.now(UTC),
                            summary_code=item.reason_code or item.type.upper(),
                            evidence={
                                "source_type": item.type,
                                "source_id": item.resource_id,
                                "transaction_id": item.transaction_id,
                                "reason_code": item.reason_code,
                                "source_version": (
                                    item.resource_id
                                    if item.resource_id.startswith("pte_")
                                    else None
                                ),
                            },
                            idempotency_key=key,
                        )
                    )
                    await self.session.flush()
                created = True
            except IntegrityError:
                # A concurrent detector won the deterministic incident key.
                continue
        if created:
            await self.session.commit()

    async def acknowledge_incident(self, incident_id: str, operator_id: str) -> Incident:
        incident = await self.session.scalar(
            select(Incident).where(Incident.id == incident_id).with_for_update()
        )
        if incident is None:
            raise ValueError("INCIDENT_NOT_FOUND")
        if incident.state == "resolved":
            raise ValueError("INCIDENT_ALREADY_RESOLVED")
        if incident.state == "acknowledged":
            raise ValueError("INCIDENT_ALREADY_ACKNOWLEDGED")
        incident.state = "acknowledged"
        incident.acknowledged_at = datetime.now(UTC)
        incident.assigned_operator_id = operator_id
        await self.session.commit()
        await self.record_action(
            operator_id=operator_id,
            action="incident_acknowledged",
            resource_type="incident",
            resource_id=incident.id,
            transaction_id=incident.transaction_id,
            reason_code="INCIDENT_ACKNOWLEDGED",
            evidence={"incident_type": incident.incident_type},
            idempotency_key=f"incident-acknowledged:{incident.id}",
        )
        return incident

    async def resolve_incident(self, incident_id: str, operator_id: str) -> Incident:
        incident = await self.session.scalar(
            select(Incident).where(Incident.id == incident_id).with_for_update()
        )
        if incident is None:
            raise ValueError("INCIDENT_NOT_FOUND")
        if incident.state == "resolved":
            return incident
        if not await self._underlying_resolved(incident):
            raise ValueError("INCIDENT_UNDERLYING_STATE_UNRESOLVED")
        incident.state = "resolved"
        incident.resolved_at = datetime.now(UTC)
        incident.assigned_operator_id = operator_id
        await self.session.commit()
        await self.record_action(
            operator_id=operator_id,
            action="incident_resolved",
            resource_type="incident",
            resource_id=incident.id,
            transaction_id=incident.transaction_id,
            reason_code="INCIDENT_RESOLVED",
            evidence={"incident_type": incident.incident_type},
            idempotency_key=f"incident-resolved:{incident.id}",
        )
        return incident

    async def _underlying_resolved(self, incident: Incident) -> bool:
        if incident.refund_id is not None:
            refund = await self.session.get(PaymentRefund, incident.refund_id)
            return refund is not None and PaymentRefundState(refund.refund_state) not in {
                PaymentRefundState.REFUND_UNCERTAIN,
                PaymentRefundState.RECONCILIATION_REQUIRED,
            }
        if incident.fulfillment_id is not None:
            execution = await self.session.get(FulfillmentExecution, incident.fulfillment_id)
            return (
                execution is not None
                and FulfillmentExecutionState(execution.execution_state)
                is not FulfillmentExecutionState.RECONCILIATION_REQUIRED
            )
        if incident.transaction_id is not None:
            transaction = await self.session.get(PaymentTransaction, incident.transaction_id)
            return (
                transaction is not None
                and PaymentTransactionState(transaction.transaction_state)
                is not PaymentTransactionState.RECONCILIATION_REQUIRED
            )
        source_id = incident.evidence.get("source_id")
        model = (
            CommerceOutboxEvent
            if incident.incident_type == "entitlement_outbox_stuck"
            else RefundOutboxEvent
        )
        event = await self.session.get(model, source_id)
        return event is not None and event.processed_at is not None

    async def compensation_review(self, case_id: str) -> dict:
        case = await self.session.get(CompensationCase, case_id)
        if case is None:
            raise ValueError("COMPENSATION_NOT_FOUND")
        attempt = await self.session.get(PaymentAttempt, case.payment_attempt_id)
        quote = await self.session.get(Quote, case.quote_id)
        return {
            "action": "approve_full_refund",
            "transaction_id": case.transaction_id,
            "payment_id": attempt.provider_payment_id if attempt is not None else None,
            "amount": case.amount_paid,
            "currency": case.currency,
            "fulfillment_id": case.fulfillment_execution_id,
            "failure_code": case.failure_code,
            "refund_on_failure": (
                quote.refund_on_fulfillment_failure if quote is not None else None
            ),
            "recommended_action": str(case.recommended_action),
            "decision_state": str(case.decision_state),
        }

    async def case_detail(self, transaction_id: str) -> dict:
        transaction = await self.session.get(PaymentTransaction, transaction_id)
        if transaction is None:
            raise ValueError("PAYMENT_TRANSACTION_NOT_FOUND")
        attempt = await self.session.scalar(
            select(PaymentAttempt)
            .where(PaymentAttempt.transaction_id == transaction.id)
            .order_by(PaymentAttempt.last_seen_at.desc())
            .limit(1)
        )
        execution = await self.session.scalar(
            select(FulfillmentExecution).where(
                FulfillmentExecution.transaction_id == transaction.id
            )
        )
        case = await self.session.scalar(
            select(CompensationCase).where(CompensationCase.transaction_id == transaction.id)
        )
        refund = await self.session.scalar(
            select(PaymentRefund).where(PaymentRefund.transaction_id == transaction.id)
        )
        incidents = list(
            (
                await self.session.scalars(
                    select(Incident).where(Incident.transaction_id == transaction.id)
                )
            ).all()
        )
        merchant = await self.session.get(Merchant, transaction.merchant_id)
        service = await self.session.get(Service, transaction.service_id)
        decisions = (
            list(
                (
                    await self.session.scalars(
                        select(OperatorDecision).where(
                            OperatorDecision.compensation_case_id == case.id
                        )
                    )
                ).all()
            )
            if case is not None
            else []
        )
        return {
            "facts": {
                "transaction_id": transaction.id,
                "account_id": transaction.account_id,
                "merchant_id": transaction.merchant_id,
                "merchant_name": merchant.name if merchant is not None else None,
                "service_id": transaction.service_id,
                "service_name": service.name if service is not None else None,
                "amount": transaction.amount,
                "currency": transaction.currency,
                "payment_state": str(transaction.transaction_state),
                "provider_order_id": transaction.provider_order_id,
                "provider_payment_id": attempt.provider_payment_id if attempt else None,
                "provider_payment_status": str(attempt.provider_status) if attempt else None,
                "fulfillment": self._execution_dict(execution),
                "compensation": self._case_dict(case),
                "refund": self._refund_dict(refund),
            },
            "derived_state": {
                "quarantined": case is not None,
                "reconciliation_required": (
                    PaymentTransactionState(transaction.transaction_state)
                    is PaymentTransactionState.RECONCILIATION_REQUIRED
                    or (
                        execution is not None
                        and FulfillmentExecutionState(execution.execution_state)
                        is FulfillmentExecutionState.RECONCILIATION_REQUIRED
                    )
                    or (
                        refund is not None
                        and PaymentRefundState(refund.refund_state)
                        is PaymentRefundState.RECONCILIATION_REQUIRED
                    )
                ),
                "open_incident_count": sum(i.state != "resolved" for i in incidents),
            },
            "operator_decisions": [
                {
                    "id": decision.id,
                    "action": decision.action,
                    "reason_code": decision.reason_code,
                    "decided_at": decision.decided_at,
                    "decision_hash": decision.decision_hash,
                }
                for decision in decisions
            ],
            "incidents": [
                {
                    "id": incident.id,
                    "severity": incident.severity,
                    "state": incident.state,
                    "reason_code": incident.summary_code,
                }
                for incident in incidents
            ],
        }

    async def metrics_summary(self) -> dict[str, int | float]:
        total = await self._count(PaymentTransaction)
        paid = await self._count(
            PaymentTransaction,
            PaymentTransaction.transaction_state == PaymentTransactionState.PAID,
        )
        gmv = (
            await self.session.scalar(
                select(func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(
                    PaymentTransaction.transaction_state == PaymentTransactionState.PAID
                )
            )
            or 0
        )
        fulfillments = await self._count(FulfillmentExecution)
        fulfillment_success = await self._count(
            FulfillmentExecution,
            FulfillmentExecution.execution_state == FulfillmentExecutionState.SUCCEEDED,
        )
        fulfillment_failures = await self._count(
            FulfillmentExecution,
            FulfillmentExecution.execution_state.in_(
                [
                    FulfillmentExecutionState.RETRYABLE_FAILURE,
                    FulfillmentExecutionState.PERMANENT_FAILURE,
                    FulfillmentExecutionState.RECONCILIATION_REQUIRED,
                ]
            ),
        )
        compensation = await self._count(CompensationCase)
        manual = await self._count(
            CompensationCase,
            CompensationCase.decision_state == CompensationDecisionState.MANUAL_REVIEW,
        )
        refunds = await self._count(PaymentRefund)
        refunded = await self._count(
            PaymentRefund, PaymentRefund.refund_state == PaymentRefundState.REFUNDED
        )
        anomalies = await self._count(Incident, Incident.state != "resolved")
        stuck = len(
            [item for item in await self.work_items() if item.type.endswith("outbox_stuck")]
        )
        evaluation_audits = list(
            (
                await self.session.scalars(
                    select(McpToolAuditEvent).where(
                        McpToolAuditEvent.tool_name == "evaluate_quote",
                        McpToolAuditEvent.result_code == "MCP_POLICY_EVALUATED",
                    )
                )
            ).all()
        )
        agent_evaluation_ids = {
            value
            for event in evaluation_audits
            if isinstance((value := event.resource_ids.get("evaluation_id")), str)
        }
        agent_evaluations = (
            list(
                (
                    await self.session.scalars(
                        select(PolicyEvaluation).where(
                            PolicyEvaluation.id.in_(agent_evaluation_ids)
                        )
                    )
                ).all()
            )
            if agent_evaluation_ids
            else []
        )
        agent_transactions = (
            list(
                (
                    await self.session.scalars(
                        select(PaymentTransaction).where(
                            PaymentTransaction.evaluation_id.in_(agent_evaluation_ids)
                        )
                    )
                ).all()
            )
            if agent_evaluation_ids
            else []
        )
        agent_transaction_ids = {transaction.id for transaction in agent_transactions}
        agent_paid = [
            transaction
            for transaction in agent_transactions
            if PaymentTransactionState(transaction.transaction_state)
            is PaymentTransactionState.PAID
        ]
        agent_executions = (
            list(
                (
                    await self.session.scalars(
                        select(FulfillmentExecution).where(
                            FulfillmentExecution.transaction_id.in_(agent_transaction_ids)
                        )
                    )
                ).all()
            )
            if agent_transaction_ids
            else []
        )
        completed_latencies = [
            (execution.completed_at - execution.started_at).total_seconds() * 1000
            for execution in agent_executions
            if execution.started_at is not None and execution.completed_at is not None
        ]
        agent_payment_failures = (
            await self.session.scalar(
                select(func.count(PaymentTransactionEvent.id)).where(
                    PaymentTransactionEvent.transaction_id.in_(agent_transaction_ids),
                    PaymentTransactionEvent.event_type == "payment_attempt_failed",
                )
            )
            if agent_transaction_ids
            else 0
        ) or 0
        agent_refund_recoveries = (
            await self.session.scalar(
                select(func.count(PaymentRefund.id)).where(
                    PaymentRefund.transaction_id.in_(agent_transaction_ids),
                    PaymentRefund.refund_state == PaymentRefundState.REFUNDED,
                )
            )
            if agent_transaction_ids
            else 0
        ) or 0
        return {
            "total_transactions": total,
            "paid_transactions": paid,
            "test_gmv_minor": int(gmv),
            "successful_fulfillments": fulfillment_success,
            "fulfillment_failures": fulfillment_failures,
            "fulfillment_success_rate": (
                fulfillment_success / fulfillments if fulfillments else 0.0
            ),
            "compensation_cases": compensation,
            "manual_review_cases": manual,
            "refunds_requested": refunds,
            "refunds_completed": refunded,
            "refund_rate": refunded / paid if paid else 0.0,
            "reconciliation_anomalies": anomalies,
            "unresolved_incidents": anomalies,
            "stuck_outbox_count": stuck,
            "agent_test_gmv_minor": sum(transaction.amount for transaction in agent_paid),
            "successful_agent_purchases": len(agent_paid),
            "agent_policy_denials": sum(
                str(evaluation.decision) == "deny" for evaluation in agent_evaluations
            ),
            "agent_payment_failures_handled": int(agent_payment_failures),
            "agent_fulfillment_successes": sum(
                FulfillmentExecutionState(execution.execution_state)
                is FulfillmentExecutionState.SUCCEEDED
                for execution in agent_executions
            ),
            "agent_refund_recoveries": int(agent_refund_recoveries),
            "average_agent_access_latency_ms": (
                sum(completed_latencies) / len(completed_latencies) if completed_latencies else 0.0
            ),
        }

    async def metrics(self) -> str:
        values = await self.metrics_summary()
        lines = ["# TYPE metergate_operational gauge"]
        lines.extend(f"metergate_{name} {value}" for name, value in values.items())
        return "\n".join(lines) + "\n"

    async def _count(self, model: type, predicate: object | None = None) -> int:
        statement = select(func.count()).select_from(model)
        if predicate is not None:
            statement = statement.where(predicate)
        return int(await self.session.scalar(statement) or 0)

    @staticmethod
    def _age(now: datetime, then: datetime) -> int:
        return max(0, int((now - then.astimezone(UTC)).total_seconds()))

    @staticmethod
    def _execution_dict(execution: FulfillmentExecution | None) -> dict | None:
        if execution is None:
            return None
        return {
            "id": execution.id,
            "state": str(execution.execution_state),
            "attempt_count": execution.attempt_count,
            "failure_code": execution.failure_code,
            "compensation_required": execution.compensation_required,
        }

    @staticmethod
    def _case_dict(case: CompensationCase | None) -> dict | None:
        if case is None:
            return None
        return {
            "id": case.id,
            "recommended_action": str(case.recommended_action),
            "decision_state": str(case.decision_state),
            "approved_refund_amount": case.approved_refund_amount,
        }

    @staticmethod
    def _refund_dict(refund: PaymentRefund | None) -> dict | None:
        if refund is None:
            return None
        return {
            "id": refund.id,
            "provider_refund_id": refund.provider_refund_id,
            "state": str(refund.refund_state),
            "provider_status": refund.provider_status,
            "reconciliation_required": refund.reconciliation_required_at is not None,
        }
