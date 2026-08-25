"""Public contracts for immutable deterministic policy evaluations."""

from datetime import datetime
from typing import Literal

from pydantic import JsonValue

from app.domain.enums import PolicyDecision
from app.domain.policy_engine import (
    PolicyCheckResult,
    PolicyReasonCode,
    PolicyRule,
)
from app.schemas.common import APIModel
from app.schemas.policies import (
    PolicyEvaluationId,
    PolicyId,
    QuoteId,
    SHA256Digest,
)


class PolicyEvaluationCreate(APIModel):
    policy_id: PolicyId
    quote_id: QuoteId


class PolicyCheckResponse(APIModel):
    rule: PolicyRule
    result: PolicyCheckResult
    reason_code: PolicyReasonCode
    details: dict[str, JsonValue]


class PolicyEvaluationResponse(APIModel):
    id: PolicyEvaluationId
    policy_id: PolicyId
    quote_id: QuoteId
    policy_hash: SHA256Digest
    quote_hash: SHA256Digest
    decision: PolicyDecision
    reason_codes: list[PolicyReasonCode]
    checks: list[PolicyCheckResponse]
    evaluated_at: datetime
    evaluation_version: Literal["1"]
    created_at: datetime
