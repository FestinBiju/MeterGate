"""Thin HTTP routes for immutable buyer policies."""

from fastapi import APIRouter, status

from app.api.v1.dependencies import (
    AuthenticatedMutationDependency,
    BuyerPolicyApplicationDependency,
    CurrentAccountDependency,
)
from app.schemas.policies import BuyerPolicyCreate, BuyerPolicyResponse

router = APIRouter(tags=["policies"])


@router.post(
    "/policies",
    response_model=BuyerPolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_policy(
    payload: BuyerPolicyCreate,
    application_service: BuyerPolicyApplicationDependency,
    current: AuthenticatedMutationDependency,
) -> BuyerPolicyResponse:
    return await application_service.create(payload, subject_ref=current.account.id)


@router.get("/policies/{policy_id}", response_model=BuyerPolicyResponse)
async def get_policy(
    policy_id: str,
    application_service: BuyerPolicyApplicationDependency,
    current: CurrentAccountDependency,
) -> BuyerPolicyResponse:
    return await application_service.get(
        policy_id,
        owned_subject_refs=frozenset((current.account.id, current.approval_identity.subject_ref)),
    )
