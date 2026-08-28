"""Thin HTTP routes for passkey approval and immutable authorizations."""

from fastapi import APIRouter, status

from app.api.v1.dependencies import (
    ApprovalApplicationDependency,
    AuthenticatedMutationDependency,
    CurrentAccountDependency,
)
from app.schemas.approvals import (
    ApprovalAssertionVerify,
    ApprovalChallengeCreate,
    ApprovalChallengeResponse,
    PurchaseAuthorizationResponse,
)

router = APIRouter(tags=["trusted-approval"])


@router.post(
    "/approval-challenges",
    response_model=ApprovalChallengeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_approval_challenge(
    payload: ApprovalChallengeCreate,
    application_service: ApprovalApplicationDependency,
    current: AuthenticatedMutationDependency,
) -> ApprovalChallengeResponse:
    return await application_service.create_challenge(
        payload,
        account_id=current.account.id,
        account_session_version=current.state.account_session_version,
    )


@router.post(
    "/approval-challenges/{challenge_id}/verify",
    response_model=PurchaseAuthorizationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def verify_approval_challenge(
    challenge_id: str,
    payload: ApprovalAssertionVerify,
    application_service: ApprovalApplicationDependency,
    current: AuthenticatedMutationDependency,
) -> PurchaseAuthorizationResponse:
    return await application_service.verify_challenge(
        challenge_id,
        payload,
        account_id=current.account.id,
        account_session_version=current.state.account_session_version,
    )


@router.get(
    "/authorizations/{authorization_id}",
    response_model=PurchaseAuthorizationResponse,
)
async def get_purchase_authorization(
    authorization_id: str,
    application_service: ApprovalApplicationDependency,
    current: CurrentAccountDependency,
) -> PurchaseAuthorizationResponse:
    return await application_service.get_authorization(
        authorization_id,
        account_id=current.account.id,
    )
