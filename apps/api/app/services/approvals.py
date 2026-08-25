"""Trusted human approval orchestration with TOCTOU-safe passkey verification."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hmac import compare_digest

from sqlalchemy.exc import IntegrityError

from app.cache.approval_challenges import (
    AllowedCredentialBinding,
    ApprovalChallengeState,
    ChallengeAlreadyExistsError,
    ChallengeAlreadyUsedError,
    ChallengeExpiredError,
    ChallengeNotFoundError,
    ChallengeStateInvalidError,
    ChallengeStore,
)
from app.domain.approval_hashing import (
    AUTHORIZATION_VERSION,
    build_approval_review_payload,
    calculate_authorization_hash,
    recompute_authorization_hash,
    review_policy_checks,
)
from app.domain.base64url import decode_base64url, encode_base64url
from app.domain.enums import ApprovalIdentityStatus, PolicyDecision, PurchaseType
from app.domain.exceptions import (
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalNotFoundError,
    ApprovalVerificationError,
    PolicyIntegrityError,
    QuoteIntegrityError,
    ResourceNotFoundError,
)
from app.domain.hashing import sha256_bytes, sha256_json
from app.domain.ids import new_approval_challenge_id, new_authorization_id
from app.domain.integrity import IntegrityStructureError
from app.domain.policy_hashing import verify_policy_integrity
from app.domain.quote_integrity import verify_quote_integrity
from app.models import (
    ApprovalIdentity,
    BuyerPolicy,
    PasskeyCredential,
    PurchaseAuthorization,
    Quote,
)
from app.repositories.approval_identities import ApprovalIdentityRepository
from app.repositories.authorizations import PurchaseAuthorizationRepository
from app.repositories.buyer_policies import BuyerPolicyRepository
from app.repositories.passkey_credentials import PasskeyCredentialRepository
from app.repositories.policy_evaluations import PolicyEvaluationRepository
from app.repositories.quotes import QuoteRepository
from app.schemas.approvals import (
    ApprovalAssertionVerify,
    ApprovalChallengeCreate,
    ApprovalChallengeResponse,
    ApprovalReview,
    AuthorizationParty,
    PurchaseAuthorizationResponse,
)
from app.schemas.policy_evaluations import PolicyEvaluationResponse
from app.services.policy_evaluations import PolicyEvaluationApplicationService
from app.services.webauthn import (
    WebAuthnBackend,
    WebAuthnCredentialDescriptor,
    WebAuthnVerificationError,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _VerifiedApprovalContext:
    policy: BuyerPolicy
    quote: Quote
    evaluation: PolicyEvaluationResponse
    review: dict[str, object]
    review_hash: str
    merchant_name: str
    service_name: str


class ApprovalApplicationService:
    """Bind an ALLOW evaluation to one verified human passkey ceremony."""

    def __init__(
        self,
        identity_repository: ApprovalIdentityRepository,
        credential_repository: PasskeyCredentialRepository,
        authorization_repository: PurchaseAuthorizationRepository,
        policy_repository: BuyerPolicyRepository,
        quote_repository: QuoteRepository,
        evaluation_repository: PolicyEvaluationRepository,
        challenge_store: ChallengeStore,
        webauthn_backend: WebAuthnBackend,
        *,
        challenge_ttl: timedelta,
        authorization_ttl: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        if challenge_ttl <= timedelta(0) or authorization_ttl <= timedelta(0):
            raise ValueError("Approval lifetimes must be positive")
        self._identity_repository = identity_repository
        self._credential_repository = credential_repository
        self._authorization_repository = authorization_repository
        self._policy_repository = policy_repository
        self._quote_repository = quote_repository
        self._evaluation_service = PolicyEvaluationApplicationService(
            policy_repository,
            quote_repository,
            evaluation_repository,
            clock=clock,
        )
        self._challenge_store = challenge_store
        self._webauthn_backend = webauthn_backend
        self._challenge_ttl = challenge_ttl
        self._authorization_ttl = authorization_ttl
        self._clock = clock

    async def create_challenge(
        self,
        payload: ApprovalChallengeCreate,
    ) -> ApprovalChallengeResponse:
        checked_at = self._read_clock()
        context = await self._verified_context(payload.evaluation_id, checked_at)
        identity = await self._get_active_identity_for_update(payload.approval_identity_id)
        self._require_subject_binding(identity, context.policy)
        context = self._bind_context_identity(context, identity)
        credentials = await self._credential_repository.list_for_identity(identity.id)
        if not credentials:
            raise ApprovalConflictError(
                "Approval identity has no registered passkey",
                "APPROVAL_PASSKEY_REQUIRED",
            )

        descriptors = tuple(self._credential_descriptor(item) for item in credentials)
        bindings = [
            AllowedCredentialBinding(
                passkey_credential_id=item.id,
                credential_id_hash=sha256_bytes(item.credential_id),
            )
            for item in credentials
        ]
        for _ in range(3):
            issued_at = self._read_clock()
            self._require_context_fresh(context, issued_at)
            expires_at = min(
                issued_at + self._challenge_ttl,
                self._as_utc(context.policy.expires_at),
                self._as_utc(context.quote.expires_at),
            )
            challenge_id = new_approval_challenge_id()
            challenge = secrets.token_bytes(32)
            public_key = self._webauthn_backend.authentication_options(
                challenge=challenge,
                allow_credentials=descriptors,
            )
            state = ApprovalChallengeState(
                challenge_id=challenge_id,
                challenge=encode_base64url(challenge),
                approval_identity_id=identity.id,
                evaluation_id=context.evaluation.id,
                policy_id=context.policy.id,
                policy_hash=context.policy.policy_hash,
                quote_id=context.quote.id,
                quote_hash=context.quote.quote_hash,
                review=context.review,
                review_hash=context.review_hash,
                amount=context.quote.amount,
                currency=context.quote.currency,
                allowed_credentials=bindings,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            pre_save_at = self._read_clock()
            self._require_context_fresh(context, pre_save_at)
            self._require_challenge_fresh(state, pre_save_at)
            try:
                await self._challenge_store.save_approval(
                    state,
                    ttl_seconds=self._challenge_ttl_seconds,
                )
            except ChallengeAlreadyExistsError:
                continue
            return ApprovalChallengeResponse(
                challenge_id=challenge_id,
                public_key=public_key,
                review=ApprovalReview.model_validate(context.review),
                review_hash=context.review_hash,
                expires_at=expires_at,
            )
        raise ApprovalIntegrityError(
            "A unique approval challenge could not be created",
            "APPROVAL_CHALLENGE_CREATION_FAILED",
        )

    async def verify_challenge(
        self,
        challenge_id: str,
        payload: ApprovalAssertionVerify,
    ) -> PurchaseAuthorizationResponse:
        state = await self._consume_challenge(challenge_id)
        challenge = self._decode_stored_challenge(state)

        identity = await self._get_active_identity_for_update(state.approval_identity_id)
        first_checked_at = self._read_clock()
        self._require_challenge_fresh(state, first_checked_at)
        context = await self._verified_context(state.evaluation_id, first_checked_at)
        self._require_subject_binding(identity, context.policy)
        context = self._bind_context_identity(context, identity)
        self._verify_challenge_binding(state, context)

        try:
            raw_credential_id = decode_base64url(
                payload.credential.rawId,
                maximum_bytes=1_024,
            )
        except ValueError as error:
            raise ApprovalVerificationError(
                "Passkey credential response is malformed",
                "APPROVAL_WEBAUTHN_VERIFICATION_FAILED",
            ) from error
        credential = await self._credential_repository.get_by_credential_id_for_update(
            raw_credential_id,
            identity_id=identity.id,
        )
        if credential is None or not self._credential_was_allowed(state, credential):
            raise ApprovalConflictError(
                "Passkey credential is not available for this approval challenge",
                "APPROVAL_CREDENTIAL_NOT_FOUND",
            )

        try:
            verification = self._webauthn_backend.verify_authentication(
                credential=payload.credential.model_dump(exclude_none=True),
                expected_challenge=challenge,
                credential_public_key=credential.public_key,
                credential_current_sign_count=credential.sign_count,
            )
        except WebAuthnVerificationError as error:
            raise ApprovalVerificationError(
                "Passkey approval could not be verified",
                "APPROVAL_WEBAUTHN_VERIFICATION_FAILED",
            ) from error
        if not compare_digest(verification.credential_id, credential.credential_id):
            raise ApprovalVerificationError(
                "Passkey approval returned a different credential",
                "APPROVAL_WEBAUTHN_VERIFICATION_FAILED",
            )
        if not verification.user_verified:
            raise ApprovalVerificationError(
                "Passkey approval did not include required user verification",
                "APPROVAL_USER_VERIFICATION_REQUIRED",
            )

        final_checked_at = self._read_clock()
        final_context = await self._verified_context(state.evaluation_id, final_checked_at)
        self._require_subject_binding(identity, final_context.policy)
        final_context = self._bind_context_identity(final_context, identity)
        self._verify_challenge_binding(state, final_context)
        authorized_at = self._read_clock()
        self._require_context_fresh(final_context, authorized_at)
        self._require_challenge_fresh(state, authorized_at)
        expires_at = min(
            authorized_at + self._authorization_ttl,
            self._as_utc(final_context.policy.expires_at),
            self._as_utc(final_context.quote.expires_at),
        )
        if expires_at <= authorized_at:
            raise ApprovalExpiredError(
                "Approval context expired during human review",
                "APPROVAL_CONTEXT_EXPIRED",
            )

        authorization_id = new_authorization_id()
        challenge_hash = sha256_bytes(challenge)
        authorization_hash = calculate_authorization_hash(
            authorization_id=authorization_id,
            authorization_version=AUTHORIZATION_VERSION,
            approval_identity_id=identity.id,
            passkey_credential_id=credential.id,
            subject_ref=identity.subject_ref,
            evaluation_id=final_context.evaluation.id,
            policy_id=final_context.policy.id,
            policy_hash=final_context.policy.policy_hash,
            quote_id=final_context.quote.id,
            quote_hash=final_context.quote.quote_hash,
            merchant_id=final_context.quote.merchant_id,
            service_id=final_context.quote.service_id,
            amount=final_context.quote.amount,
            currency=final_context.quote.currency,
            purchase_type=final_context.quote.purchase_type,
            review_hash=final_context.review_hash,
            challenge_hash=challenge_hash,
            authorized_at=authorized_at,
            expires_at=expires_at,
        )
        authorization = PurchaseAuthorization(
            id=authorization_id,
            approval_identity_id=identity.id,
            passkey_credential_id=credential.id,
            evaluation_id=final_context.evaluation.id,
            policy_id=final_context.policy.id,
            policy_hash=final_context.policy.policy_hash,
            quote_id=final_context.quote.id,
            quote_hash=final_context.quote.quote_hash,
            merchant_id=final_context.quote.merchant_id,
            service_id=final_context.quote.service_id,
            subject_ref=identity.subject_ref,
            amount=final_context.quote.amount,
            currency=final_context.quote.currency,
            purchase_type=final_context.quote.purchase_type,
            review_hash=final_context.review_hash,
            challenge_hash=challenge_hash,
            authorized_at=authorized_at,
            expires_at=expires_at,
            authorization_version=AUTHORIZATION_VERSION,
            authorization_hash=authorization_hash,
        )
        try:
            persisted = await self._authorization_repository.create_with_locked_credential_update(
                authorization,
                credential=credential,
                new_sign_count=verification.new_sign_count,
                last_used_at=authorized_at,
            )
        except IntegrityError as error:
            raise ApprovalConflictError(
                "Approval challenge cannot create another authorization",
                "APPROVAL_CHALLENGE_ALREADY_USED",
            ) from error
        return self._authorization_response(persisted, final_context.quote)

    async def get_authorization(
        self,
        authorization_id: str,
    ) -> PurchaseAuthorizationResponse:
        authorization = await self._authorization_repository.get(authorization_id)
        if authorization is None:
            raise ApprovalNotFoundError(
                f"Purchase authorization '{authorization_id}' was not found",
                "APPROVAL_AUTHORIZATION_NOT_FOUND",
            )
        if not compare_digest(
            recompute_authorization_hash(authorization),
            authorization.authorization_hash,
        ):
            raise ApprovalIntegrityError(
                "Stored purchase authorization failed integrity verification",
                "INTEGRITY_AUTHORIZATION_HASH_MISMATCH",
            )
        quote = await self._quote_repository.get(authorization.quote_id)
        if quote is None or not self._authorization_matches_quote(authorization, quote):
            raise ApprovalIntegrityError(
                "Stored purchase authorization has invalid quote binding",
                "INTEGRITY_AUTHORIZATION_DATA_INVALID",
            )
        return self._authorization_response(authorization, quote)

    async def _verified_context(
        self,
        evaluation_id: str,
        now: datetime,
    ) -> _VerifiedApprovalContext:
        try:
            evaluation = await self._evaluation_service.get(evaluation_id)
        except ResourceNotFoundError as error:
            raise ApprovalNotFoundError(
                f"Policy evaluation '{evaluation_id}' was not found",
                "APPROVAL_EVALUATION_NOT_FOUND",
            ) from error
        if evaluation.decision is not PolicyDecision.ALLOW:
            raise ApprovalConflictError(
                "Only an ALLOW policy evaluation can reach human approval",
                "APPROVAL_EVALUATION_NOT_ALLOWED",
            )

        policy = await self._policy_repository.get(evaluation.policy_id)
        quote = await self._quote_repository.get(evaluation.quote_id)
        if policy is None or quote is None:
            raise ApprovalIntegrityError(
                "Approval evaluation parents are missing",
                "INTEGRITY_POLICY_EVALUATION_DATA_INVALID",
            )
        try:
            policy_verification = verify_policy_integrity(policy)
            quote_verification = verify_quote_integrity(quote)
        except IntegrityStructureError as error:
            raise ApprovalIntegrityError(
                "Approval context contains structurally invalid evidence",
                "INTEGRITY_APPROVAL_CONTEXT_INVALID",
            ) from error
        if not policy_verification.hash_matches:
            raise PolicyIntegrityError(policy.id)
        if not quote_verification.hash_matches:
            raise QuoteIntegrityError(quote.id)

        policy_expires_at = self._as_utc(policy.expires_at)
        quote_expires_at = self._as_utc(quote.expires_at)
        if now >= policy_expires_at:
            raise ApprovalExpiredError(
                "Buyer policy expired before approval completed",
                "APPROVAL_POLICY_EXPIRED",
            )
        if now >= quote_expires_at:
            raise ApprovalExpiredError(
                "Quote expired before approval completed",
                "APPROVAL_QUOTE_EXPIRED",
            )
        if now < self._as_utc(policy.issued_at) or now < self._as_utc(quote.issued_at):
            raise ApprovalIntegrityError(
                "Approval context timestamps are invalid",
                "INTEGRITY_APPROVAL_CONTEXT_INVALID",
            )

        merchant_name, service_name = self._snapshot_names(quote)
        checks = review_policy_checks(
            [check.model_dump(mode="json") for check in evaluation.checks],
            currency=quote.currency,
            merchant_name=merchant_name,
            service_name=service_name,
        )
        review = build_approval_review_payload(
            evaluation_id=evaluation.id,
            evaluation_version=evaluation.evaluation_version,
            policy_id=policy.id,
            policy_hash=policy.policy_hash,
            quote_id=quote.id,
            quote_hash=quote.quote_hash,
            subject_ref=policy.subject_ref,
            approval_identity_id="",  # replaced after identity binding
            merchant_id=quote.merchant_id,
            merchant_name=merchant_name,
            service_id=quote.service_id,
            service_name=service_name,
            amount=quote.amount,
            currency=quote.currency,
            purchase_type=quote.purchase_type,
            quote_expires_at=quote_expires_at,
            policy_expires_at=policy_expires_at,
            policy_checks=checks,
        )
        return _VerifiedApprovalContext(
            policy=policy,
            quote=quote,
            evaluation=evaluation,
            review=review,
            review_hash="",
            merchant_name=merchant_name,
            service_name=service_name,
        )

    @staticmethod
    def _bind_context_identity(
        context: _VerifiedApprovalContext,
        identity: ApprovalIdentity,
    ) -> _VerifiedApprovalContext:
        review = dict(context.review)
        review["approval_identity_id"] = identity.id
        return _VerifiedApprovalContext(
            policy=context.policy,
            quote=context.quote,
            evaluation=context.evaluation,
            review=review,
            review_hash=sha256_json(review),
            merchant_name=context.merchant_name,
            service_name=context.service_name,
        )

    async def _get_active_identity(self, identity_id: str) -> ApprovalIdentity:
        identity = await self._identity_repository.get(identity_id)
        return self._validate_active_identity(identity, identity_id)

    async def _get_active_identity_for_update(self, identity_id: str) -> ApprovalIdentity:
        identity = await self._identity_repository.get_for_update(identity_id)
        return self._validate_active_identity(identity, identity_id)

    @staticmethod
    def _validate_active_identity(
        identity: ApprovalIdentity | None,
        identity_id: str,
    ) -> ApprovalIdentity:
        if identity is None:
            raise ApprovalNotFoundError(
                f"Approval identity '{identity_id}' was not found",
                "APPROVAL_IDENTITY_NOT_FOUND",
            )
        if ApprovalIdentityStatus(identity.status) is not ApprovalIdentityStatus.ACTIVE:
            raise ApprovalConflictError(
                f"Approval identity '{identity_id}' is disabled",
                "APPROVAL_IDENTITY_DISABLED",
            )
        return identity

    @staticmethod
    def _require_subject_binding(identity: ApprovalIdentity, policy: BuyerPolicy) -> None:
        if not compare_digest(identity.subject_ref, policy.subject_ref):
            raise ApprovalConflictError(
                "Approval identity subject does not match the buyer policy subject",
                "APPROVAL_IDENTITY_SUBJECT_MISMATCH",
            )

    def _require_context_fresh(
        self,
        context: _VerifiedApprovalContext,
        now: datetime,
    ) -> None:
        if now >= self._as_utc(context.policy.expires_at):
            raise ApprovalExpiredError(
                "Buyer policy expired before approval completed",
                "APPROVAL_POLICY_EXPIRED",
            )
        if now >= self._as_utc(context.quote.expires_at):
            raise ApprovalExpiredError(
                "Quote expired before approval completed",
                "APPROVAL_QUOTE_EXPIRED",
            )

    @staticmethod
    def _require_challenge_fresh(state: ApprovalChallengeState, now: datetime) -> None:
        if now >= state.expires_at:
            raise ApprovalExpiredError(
                "Approval challenge expired before human verification completed",
                "APPROVAL_CHALLENGE_EXPIRED",
            )

    @staticmethod
    def _credential_descriptor(credential: PasskeyCredential) -> WebAuthnCredentialDescriptor:
        return WebAuthnCredentialDescriptor(
            credential_id=credential.credential_id,
            transports=tuple(credential.transports or ()),
        )

    @staticmethod
    def _credential_was_allowed(
        state: ApprovalChallengeState,
        credential: PasskeyCredential,
    ) -> bool:
        credential_hash = sha256_bytes(credential.credential_id)
        return any(
            binding.passkey_credential_id == credential.id
            and compare_digest(binding.credential_id_hash, credential_hash)
            for binding in state.allowed_credentials
        )

    async def _consume_challenge(self, challenge_id: str) -> ApprovalChallengeState:
        try:
            return await self._challenge_store.consume_approval(challenge_id)
        except ChallengeNotFoundError as error:
            raise ApprovalNotFoundError(
                str(error),
                "APPROVAL_CHALLENGE_NOT_FOUND",
            ) from error
        except ChallengeExpiredError as error:
            raise ApprovalExpiredError(
                str(error),
                "APPROVAL_CHALLENGE_EXPIRED",
            ) from error
        except ChallengeAlreadyUsedError as error:
            raise ApprovalConflictError(
                str(error),
                "APPROVAL_CHALLENGE_ALREADY_USED",
            ) from error
        except ChallengeStateInvalidError as error:
            raise ApprovalIntegrityError(
                str(error),
                "APPROVAL_CHALLENGE_STATE_INVALID",
            ) from error

    @staticmethod
    def _decode_stored_challenge(state: ApprovalChallengeState) -> bytes:
        try:
            return decode_base64url(state.challenge, maximum_bytes=64)
        except ValueError as error:
            raise ApprovalIntegrityError(
                "Stored approval challenge is invalid",
                "APPROVAL_CHALLENGE_STATE_INVALID",
            ) from error

    @staticmethod
    def _verify_challenge_binding(
        state: ApprovalChallengeState,
        context: _VerifiedApprovalContext,
    ) -> None:
        if (
            state.evaluation_id != context.evaluation.id
            or state.policy_id != context.policy.id
            or not compare_digest(state.policy_hash, context.policy.policy_hash)
            or state.quote_id != context.quote.id
            or not compare_digest(state.quote_hash, context.quote.quote_hash)
            or state.amount != context.quote.amount
            or state.currency != context.quote.currency
            or state.review != context.review
            or not compare_digest(state.review_hash, context.review_hash)
        ):
            raise ApprovalConflictError(
                "Approval review no longer matches its server-side binding",
                "APPROVAL_REVIEW_HASH_MISMATCH",
            )

    @staticmethod
    def _snapshot_names(quote: Quote) -> tuple[str, str]:
        try:
            merchant_name = quote.service_snapshot["merchant"]["name"]
            service_name = quote.service_snapshot["service"]["name"]
        except (KeyError, TypeError) as error:
            raise QuoteIntegrityError(quote.id, "INTEGRITY_QUOTE_DATA_INVALID") from error
        if not isinstance(merchant_name, str) or not isinstance(service_name, str):
            raise QuoteIntegrityError(quote.id, "INTEGRITY_QUOTE_DATA_INVALID")
        return merchant_name, service_name

    @staticmethod
    def _authorization_matches_quote(
        authorization: PurchaseAuthorization,
        quote: Quote,
    ) -> bool:
        try:
            verification = verify_quote_integrity(quote)
        except IntegrityStructureError:
            return False
        return (
            verification.hash_matches
            and authorization.quote_hash == quote.quote_hash
            and authorization.merchant_id == quote.merchant_id
            and authorization.service_id == quote.service_id
            and authorization.amount == quote.amount
            and authorization.currency == quote.currency
            and PurchaseType(authorization.purchase_type) is PurchaseType(quote.purchase_type)
        )

    def _authorization_response(
        self,
        authorization: PurchaseAuthorization,
        quote: Quote,
    ) -> PurchaseAuthorizationResponse:
        merchant_name, service_name = self._snapshot_names(quote)
        now = self._read_clock()
        return PurchaseAuthorizationResponse(
            id=authorization.id,
            state="active" if now < self._as_utc(authorization.expires_at) else "expired",
            subject_ref=authorization.subject_ref,
            merchant=AuthorizationParty(id=authorization.merchant_id, name=merchant_name),
            service=AuthorizationParty(id=authorization.service_id, name=service_name),
            amount=authorization.amount,
            currency=authorization.currency,
            purchase_type=authorization.purchase_type,
            evaluation_id=authorization.evaluation_id,
            policy_hash=authorization.policy_hash,
            quote_hash=authorization.quote_hash,
            review_hash=authorization.review_hash,
            authorized_at=authorization.authorized_at,
            expires_at=authorization.expires_at,
            authorization_version=authorization.authorization_version,
            authorization_hash=authorization.authorization_hash,
        )

    @property
    def _challenge_ttl_seconds(self) -> int:
        return int(self._challenge_ttl.total_seconds())

    def _read_clock(self) -> datetime:
        return self._as_utc(self._clock())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Approval timestamps must be timezone-aware")
        return value.astimezone(UTC)
