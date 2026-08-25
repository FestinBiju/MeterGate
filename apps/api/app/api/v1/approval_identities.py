"""Thin HTTP routes for approval identities and passkey registration."""

from fastapi import APIRouter, status

from app.api.v1.dependencies import (
    CurrentAccountDependency,
    PasskeyApplicationDependency,
    RecentAuthenticationDependency,
)
from app.schemas.approvals import (
    ApprovalIdentityResponse,
    PasskeyRegistrationOptionsResponse,
    PasskeyRegistrationResponse,
    PasskeyRegistrationVerify,
)

router = APIRouter(tags=["approval-identities"])


@router.get(
    "/approval-identities/{identity_id}",
    response_model=ApprovalIdentityResponse,
)
async def get_approval_identity(
    identity_id: str,
    application_service: PasskeyApplicationDependency,
    current: CurrentAccountDependency,
) -> ApprovalIdentityResponse:
    return await application_service.get_identity(identity_id, account_id=current.account.id)


@router.post(
    "/approval-identities/{identity_id}/passkeys/options",
    response_model=PasskeyRegistrationOptionsResponse,
)
async def create_passkey_registration_options(
    identity_id: str,
    application_service: PasskeyApplicationDependency,
    current: RecentAuthenticationDependency,
) -> PasskeyRegistrationOptionsResponse:
    return await application_service.registration_options(
        identity_id,
        account_id=current.account.id,
        session_id=current.state.session_id,
    )


@router.post(
    "/approval-identities/{identity_id}/passkeys/verify",
    response_model=PasskeyRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def verify_passkey_registration(
    identity_id: str,
    payload: PasskeyRegistrationVerify,
    application_service: PasskeyApplicationDependency,
    current: RecentAuthenticationDependency,
) -> PasskeyRegistrationResponse:
    return await application_service.verify_registration(
        identity_id,
        payload,
        account_id=current.account.id,
        account_session_version=current.state.account_session_version,
        session_id=current.state.session_id,
    )
