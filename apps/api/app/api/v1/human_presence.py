"""Cookie-authenticated WebAuthn endpoints for proof of human presence."""

from fastapi import APIRouter, Response, status

from app.api.v1.dependencies import (
    AllowedOriginDependency,
    AuthenticatedMutationDependency,
    CurrentAccountDependency,
    HumanPresenceServiceDependency,
    disable_private_caching,
)
from app.schemas.human_presence import (
    HumanPresenceChallengeCreate,
    HumanPresenceChallengeResponse,
    HumanPresenceProofResponse,
    HumanPresenceStatusResponse,
    HumanPresenceVerify,
)

router = APIRouter(prefix="/human-presence", tags=["human presence"])


@router.post(
    "/challenge", response_model=HumanPresenceChallengeResponse, status_code=status.HTTP_201_CREATED
)
async def create_human_presence_challenge(
    payload: HumanPresenceChallengeCreate,
    response: Response,
    service: HumanPresenceServiceDependency,
    current: AuthenticatedMutationDependency,
    origin: AllowedOriginDependency,
) -> HumanPresenceChallengeResponse:
    disable_private_caching(response)
    return await service.create_challenge(
        payload,
        account_id=current.account.id,
        session_id=current.state.session_id,
        origin=origin,
    )


@router.post(
    "/{challenge_id}/verify",
    response_model=HumanPresenceProofResponse,
    status_code=status.HTTP_201_CREATED,
)
async def verify_human_presence(
    challenge_id: str,
    payload: HumanPresenceVerify,
    response: Response,
    service: HumanPresenceServiceDependency,
    current: AuthenticatedMutationDependency,
    origin: AllowedOriginDependency,
) -> HumanPresenceProofResponse:
    disable_private_caching(response)
    return await service.verify(
        challenge_id,
        payload,
        account_id=current.account.id,
        session_id=current.state.session_id,
        origin=origin,
        account_session_version=current.state.account_session_version,
    )


@router.get("/status", response_model=HumanPresenceStatusResponse)
async def human_presence_status(
    response: Response,
    service: HumanPresenceServiceDependency,
    current: CurrentAccountDependency,
) -> HumanPresenceStatusResponse:
    disable_private_caching(response)
    return await service.status(account_id=current.account.id, session_id=current.state.session_id)
