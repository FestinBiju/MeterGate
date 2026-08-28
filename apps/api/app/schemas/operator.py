"""Safe operator control-plane contracts."""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import APIModel, ORMResponseModel


class OperatorContextResponse(APIModel):
    account_id: str
    role: Literal["operator", "admin"]


class OperatorDecisionRequest(APIModel):
    reason_code: Literal[
        "OPERATOR_APPROVE_CONFIRMED_NON_DELIVERY",
        "OPERATOR_REJECT_VALUE_ALREADY_DELIVERED",
        "OPERATOR_REJECT_DUPLICATE_REQUEST",
        "OPERATOR_ESCALATE_PROVIDER_CONFLICT",
        "OPERATOR_ESCALATE_EVIDENCE_INCOMPLETE",
    ]
    note: dict[str, Any] | None = Field(default=None)


class OperatorDecisionResponse(APIModel):
    id: str
    compensation_case_id: str
    action: Literal["approve", "reject"]
    reason_code: str
    decided_at: datetime
    decision_hash: str
    approved_refund_amount: int | None


class WorkItem(APIModel):
    type: str
    severity: Literal["info", "warning", "high", "critical"]
    state: str
    resource_id: str
    transaction_id: str | None = None
    account_id: str | None = None
    merchant_id: str | None = None
    service_id: str | None = None
    age_seconds: int
    reason_code: str | None = None


class IncidentResponse(ORMResponseModel):
    id: str
    incident_type: str
    severity: str
    state: str
    summary_code: str
    opened_at: datetime
    transaction_id: str | None
    refund_id: str | None
    fulfillment_id: str | None
    compensation_id: str | None
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    assigned_operator_id: str | None
    evidence: dict[str, Any]


class SystemHealthResponse(APIModel):
    postgresql: str
    redis: str
    workers: list[dict[str, Any]]
    queues: dict[str, Any]
