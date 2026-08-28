"""Strict public contracts for passkey-first account authentication."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints

from app.domain.enums import AccountStatus, ApprovalIdentityStatus
from app.schemas.approvals import (
    ApprovalChallengeId,
    ApprovalIdentityId,
    BrowserCredential,
    PasskeyCredentialId,
)
from app.schemas.common import AccountId, APIModel, Name

CSRFToken = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]


class SignupOptionsCreate(APIModel):
    display_name: Name


class AuthCeremonyOptionsResponse(APIModel):
    challenge_id: ApprovalChallengeId
    public_key: dict[str, JsonValue]
    expires_at: datetime


class AuthCeremonyVerify(APIModel):
    challenge_id: ApprovalChallengeId
    credential: BrowserCredential


class AccountResponse(APIModel):
    id: AccountId
    display_name: Name
    status: AccountStatus
    created_at: datetime
    updated_at: datetime


class SessionApprovalIdentityResponse(APIModel):
    id: ApprovalIdentityId
    status: ApprovalIdentityStatus
    credential_count: int = Field(ge=1)


class AuthSessionResponse(APIModel):
    reason_code: Literal["AUTH_LOGGED_IN"] = "AUTH_LOGGED_IN"
    account: AccountResponse
    approval_identity: SessionApprovalIdentityResponse
    auth_method: Literal["passkey"] = "passkey"
    authenticated_at: datetime
    expires_at: datetime
    csrf_token: CSRFToken


class AuthLogoutResponse(APIModel):
    reason_code: Literal["AUTH_LOGGED_OUT"] = "AUTH_LOGGED_OUT"


class AuthPasskeyResponse(APIModel):
    id: PasskeyCredentialId
    transports: list[str] | None
    created_at: datetime
    last_used_at: datetime | None
    current: bool


class AuthPasskeyListResponse(APIModel):
    credentials: list[AuthPasskeyResponse]
