"""Thin HTTP routes for deterministic policy evaluation evidence."""

from fastapi import APIRouter, status

from app.api.v1.dependencies import (
    AuthenticatedMutationDependency,
    CurrentAccountDependency,
    PolicyEvaluationApplicationDependency,
)
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
    current: AuthenticatedMutationDependency,
) -> PolicyEvaluationResponse:
    return await application_service.create(
        payload,
        owned_subject_refs=frozenset((current.account.id, current.approval_identity.subject_ref)),
    )


@router.get(
    "/policy-evaluations/{evaluation_id}",
    response_model=PolicyEvaluationResponse,
)
async def get_policy_evaluation(
    evaluation_id: str,
    application_service: PolicyEvaluationApplicationDependency,
    current: CurrentAccountDependency,
) -> PolicyEvaluationResponse:
    return await application_service.get(
        evaluation_id,
        owned_subject_refs=frozenset((current.account.id, current.approval_identity.subject_ref)),
    )
