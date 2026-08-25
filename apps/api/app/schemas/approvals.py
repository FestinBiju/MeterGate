"""Public contracts for trusted passkey approval and its audit artifact."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints

from app.domain.enums import ApprovalIdentityStatus, PurchaseType
from app.domain.policy_engine import PolicyCheckResult, PolicyReasonCode, PolicyRule
from app.schemas.common import APIModel, CurrencyCode, Name
from app.schemas.policies import (
    PolicyEvaluationId,
    PolicyId,
    QuoteId,
    SHA256Digest,
    SubjectReference,
)
from app.schemas.quotes import QuoteAmount

ApprovalIdentityId = Annotated[
    str,
    StringConstraints(pattern=r"^aid_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
PasskeyCredentialId = Annotated[
    str,
    StringConstraints(pattern=r"^pkc_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
ApprovalChallengeId = Annotated[
    str,
    StringConstraints(pattern=r"^ach_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
AuthorizationId = Annotated[
    str,
    StringConstraints(pattern=r"^aut_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
CanonicalTimestamp = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
            r"[0-9]{2}\.[0-9]{6}Z$"
        )
    ),
]


class ApprovalIdentityCreate(APIModel):
    subject_ref: SubjectReference
    display_name: Name


class ApprovalIdentityResponse(APIModel):
    id: ApprovalIdentityId
    subject_ref: SubjectReference
    display_name: Name
    status: ApprovalIdentityStatus
    credential_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class BrowserCredential(APIModel):
    """Opaque browser response; cryptographic parsing belongs to the WebAuthn backend."""

    id: str = Field(min_length=1, max_length=4_096)
    rawId: str = Field(min_length=1, max_length=4_096)  # noqa: N815
    type: Literal["public-key"]
    response: dict[str, JsonValue]
    clientExtensionResults: dict[str, JsonValue] = Field(  # noqa: N815
        default_factory=dict
    )
    authenticatorAttachment: str | None = None  # noqa: N815


class PasskeyRegistrationOptionsResponse(APIModel):
    challenge_id: ApprovalChallengeId
    public_key: dict[str, JsonValue]
    expires_at: datetime


class PasskeyRegistrationVerify(APIModel):
    challenge_id: ApprovalChallengeId
    credential: BrowserCredential


class PasskeyCredentialResponse(APIModel):
    id: PasskeyCredentialId
    transports: list[str] | None
    created_at: datetime
    last_used_at: datetime | None


class PasskeyRegistrationResponse(APIModel):
    status: Literal["registered"] = "registered"
    credential: PasskeyCredentialResponse


class ApprovalChallengeCreate(APIModel):
    evaluation_id: PolicyEvaluationId
    approval_identity_id: ApprovalIdentityId


class ApprovalReviewParty(APIModel):
    id: str
    name: Name


class ApprovalReviewPolicyCheck(APIModel):
    rule: PolicyRule
    result: PolicyCheckResult
    reason_code: PolicyReasonCode
    details: dict[str, JsonValue]
    explanation: str = Field(min_length=1, max_length=500)


class ApprovalReview(APIModel):
    review_version: Literal["1"]
    evaluation_id: PolicyEvaluationId
    evaluation_version: Literal["1"]
    policy_id: PolicyId
    policy_hash: SHA256Digest
    quote_id: QuoteId
    quote_hash: SHA256Digest
    subject_ref: SubjectReference
    approval_identity_id: ApprovalIdentityId
    merchant: ApprovalReviewParty
    service: ApprovalReviewParty
    amount: QuoteAmount
    currency: CurrencyCode
    purchase_type: PurchaseType
    quote_expires_at: CanonicalTimestamp
    policy_expires_at: CanonicalTimestamp
    policy_checks: list[ApprovalReviewPolicyCheck] = Field(min_length=10, max_length=10)


class ApprovalChallengeResponse(APIModel):
    challenge_id: ApprovalChallengeId
    public_key: dict[str, JsonValue]
    review: ApprovalReview
    review_hash: SHA256Digest
    expires_at: datetime


class ApprovalAssertionVerify(APIModel):
    credential: BrowserCredential


class AuthorizationParty(APIModel):
    id: str
    name: Name


class PurchaseAuthorizationResponse(APIModel):
    reason_code: Literal["APPROVAL_AUTHORIZED"] = "APPROVAL_AUTHORIZED"
    id: AuthorizationId
    state: Literal["active", "expired"]
    subject_ref: SubjectReference
    merchant: AuthorizationParty
    service: AuthorizationParty
    amount: QuoteAmount
    currency: CurrencyCode
    purchase_type: PurchaseType
    evaluation_id: PolicyEvaluationId
    policy_hash: SHA256Digest
    quote_hash: SHA256Digest
    review_hash: SHA256Digest
    authorized_at: datetime
    expires_at: datetime
    authorization_version: Literal["1"]
    authorization_hash: SHA256Digest
