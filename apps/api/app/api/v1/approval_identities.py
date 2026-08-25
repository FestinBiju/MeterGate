"""Thin HTTP routes for approval identities and passkey registration."""

from fastapi import APIRouter, status

from app.api.v1.dependencies import PasskeyApplicationDependency
from app.schemas.approvals import (
    ApprovalIdentityCreate,
    ApprovalIdentityResponse,
    PasskeyRegistrationOptionsResponse,
    PasskeyRegistrationResponse,
    PasskeyRegistrationVerify,
)

router = APIRouter(tags=["approval-identities"])


@router.post(
    "/approval-identities",
    response_model=ApprovalIdentityResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_approval_identity(
    payload: ApprovalIdentityCreate,
    application_service: PasskeyApplicationDependency,
) -> ApprovalIdentityResponse:
    return await application_service.create_identity(payload)


@router.get(
    "/approval-identities/{identity_id}",
    response_model=ApprovalIdentityResponse,
)
async def get_approval_identity(
    identity_id: str,
    application_service: PasskeyApplicationDependency,
) -> ApprovalIdentityResponse:
    return await application_service.get_identity(identity_id)


@router.post(
    "/approval-identities/{identity_id}/passkeys/options",
    response_model=PasskeyRegistrationOptionsResponse,
)
async def create_passkey_registration_options(
    identity_id: str,
    application_service: PasskeyApplicationDependency,
) -> PasskeyRegistrationOptionsResponse:
    return await application_service.registration_options(identity_id)


@router.post(
    "/approval-identities/{identity_id}/passkeys/verify",
    response_model=PasskeyRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def verify_passkey_registration(
    identity_id: str,
    payload: PasskeyRegistrationVerify,
    application_service: PasskeyApplicationDependency,
) -> PasskeyRegistrationResponse:
    return await application_service.verify_registration(identity_id, payload)
