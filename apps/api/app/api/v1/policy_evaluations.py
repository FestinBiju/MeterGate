"""Thin HTTP routes for deterministic policy evaluation evidence."""

from fastapi import APIRouter, status

from app.api.v1.dependencies import PolicyEvaluationApplicationDependency
from app.schemas.policy_evaluations import (
    PolicyEvaluationCreate,
    PolicyEvaluationResponse,
)

router = APIRouter(tags=["policy-evaluations"])


@router.post(
    "/policy-evaluations",
    response_model=PolicyEvaluationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_policy_evaluation(
    payload: PolicyEvaluationCreate,
    application_service: PolicyEvaluationApplicationDependency,
) -> PolicyEvaluationResponse:
    return await application_service.create(payload)


@router.get(
    "/policy-evaluations/{evaluation_id}",
    response_model=PolicyEvaluationResponse,
)
async def get_policy_evaluation(
    evaluation_id: str,
    application_service: PolicyEvaluationApplicationDependency,
) -> PolicyEvaluationResponse:
    return await application_service.get(evaluation_id)
