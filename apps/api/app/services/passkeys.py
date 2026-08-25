"""Approval-identity and real WebAuthn passkey registration orchestration."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.cache.approval_challenges import (
    ChallengeAlreadyExistsError,
    ChallengeAlreadyUsedError,
    ChallengeExpiredError,
    ChallengeNotFoundError,
    ChallengeStateInvalidError,
    ChallengeStore,
    RegistrationChallengeState,
)
from app.domain.base64url import decode_base64url, encode_base64url
from app.domain.enums import ApprovalIdentityStatus
from app.domain.exceptions import (
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalNotFoundError,
    ApprovalVerificationError,
)
from app.domain.ids import new_approval_challenge_id
from app.models import ApprovalIdentity, PasskeyCredential
from app.repositories.approval_identities import ApprovalIdentityRepository
from app.repositories.passkey_credentials import PasskeyCredentialRepository
from app.schemas.approvals import (
    ApprovalIdentityCreate,
    ApprovalIdentityResponse,
    PasskeyCredentialResponse,
    PasskeyRegistrationOptionsResponse,
    PasskeyRegistrationResponse,
    PasskeyRegistrationVerify,
)
from app.services.webauthn import (
    WebAuthnBackend,
    WebAuthnCredentialDescriptor,
    WebAuthnVerificationError,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class PasskeyApplicationService:
    """Create scoped principals and register standards-verified credentials."""

    def __init__(
        self,
        identity_repository: ApprovalIdentityRepository,
        credential_repository: PasskeyCredentialRepository,
        challenge_store: ChallengeStore,
        webauthn_backend: WebAuthnBackend,
        *,
        challenge_ttl: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        if challenge_ttl <= timedelta(0):
            raise ValueError("WebAuthn challenge lifetime must be positive")
        self._identity_repository = identity_repository
        self._credential_repository = credential_repository
        self._challenge_store = challenge_store
        self._webauthn_backend = webauthn_backend
        self._challenge_ttl = challenge_ttl
        self._clock = clock

    async def create_identity(
        self,
        payload: ApprovalIdentityCreate,
    ) -> ApprovalIdentityResponse:
        identity = ApprovalIdentity(
            subject_ref=payload.subject_ref,
            display_name=payload.display_name,
            status=ApprovalIdentityStatus.ACTIVE,
        )
        try:
            persisted = await self._identity_repository.create(identity)
        except IntegrityError as error:
            raise ApprovalConflictError(
                "An approval identity already exists for this policy subject",
                "APPROVAL_IDENTITY_SUBJECT_EXISTS",
            ) from error
        return await self._identity_response(persisted)

    async def get_identity(self, identity_id: str) -> ApprovalIdentityResponse:
        identity = await self._get_identity(identity_id)
        return await self._identity_response(identity)

    async def registration_options(
        self,
        identity_id: str,
    ) -> PasskeyRegistrationOptionsResponse:
        identity = await self._get_active_identity(identity_id)
        credentials = await self._credential_repository.list_for_identity(identity.id)
        descriptors = tuple(
            WebAuthnCredentialDescriptor(
                credential_id=credential.credential_id,
                transports=tuple(credential.transports or ()),
            )
            for credential in credentials
        )

        for _ in range(3):
            issued_at = self._read_clock()
            expires_at = issued_at + self._challenge_ttl
            challenge_id = new_approval_challenge_id()
            challenge = secrets.token_bytes(32)
            state = RegistrationChallengeState(
                challenge_id=challenge_id,
                challenge=encode_base64url(challenge),
                approval_identity_id=identity.id,
                user_handle=encode_base64url(identity.webauthn_user_handle),
                issued_at=issued_at,
                expires_at=expires_at,
            )
            public_key = self._webauthn_backend.registration_options(
                challenge=challenge,
                user_handle=identity.webauthn_user_handle,
                user_name=identity.subject_ref,
                user_display_name=identity.display_name,
                exclude_credentials=descriptors,
            )
            try:
                await self._challenge_store.save_registration(
                    state,
                    ttl_seconds=self._challenge_ttl_seconds,
                )
            except ChallengeAlreadyExistsError:
                continue
            return PasskeyRegistrationOptionsResponse(
                challenge_id=challenge_id,
                public_key=public_key,
                expires_at=expires_at,
            )
        raise ApprovalIntegrityError(
            "A unique passkey registration challenge could not be created",
            "PASSKEY_CHALLENGE_CREATION_FAILED",
        )

    async def verify_registration(
        self,
        identity_id: str,
        payload: PasskeyRegistrationVerify,
    ) -> PasskeyRegistrationResponse:
        try:
            state = await self._challenge_store.consume_registration(payload.challenge_id)
        except ChallengeNotFoundError as error:
            raise ApprovalNotFoundError(str(error), "PASSKEY_CHALLENGE_NOT_FOUND") from error
        except ChallengeExpiredError as error:
            raise ApprovalExpiredError(str(error), "PASSKEY_CHALLENGE_EXPIRED") from error
        except ChallengeAlreadyUsedError as error:
            raise ApprovalConflictError(
                str(error),
                "PASSKEY_CHALLENGE_ALREADY_USED",
            ) from error
        except ChallengeStateInvalidError as error:
            raise ApprovalIntegrityError(
                str(error),
                "PASSKEY_CHALLENGE_STATE_INVALID",
            ) from error

        if state.approval_identity_id != identity_id:
            raise ApprovalConflictError(
                "Passkey challenge belongs to a different approval identity",
                "PASSKEY_IDENTITY_BINDING_MISMATCH",
            )
        identity = await self._get_active_identity(identity_id)
        if encode_base64url(identity.webauthn_user_handle) != state.user_handle:
            raise ApprovalIntegrityError(
                "Passkey challenge identity binding is invalid",
                "PASSKEY_CHALLENGE_STATE_INVALID",
            )

        try:
            challenge = decode_base64url(state.challenge, maximum_bytes=64)
            verification = self._webauthn_backend.verify_registration(
                credential=payload.credential.model_dump(exclude_none=True),
                expected_challenge=challenge,
            )
        except (ValueError, WebAuthnVerificationError) as error:
            raise ApprovalVerificationError(
                "Passkey registration could not be verified",
                "PASSKEY_REGISTRATION_VERIFICATION_FAILED",
            ) from error
        if not verification.user_verified:
            raise ApprovalVerificationError(
                "Passkey registration did not include required user verification",
                "APPROVAL_USER_VERIFICATION_REQUIRED",
            )

        credential = PasskeyCredential(
            approval_identity_id=identity.id,
            credential_id=verification.credential_id,
            public_key=verification.public_key,
            sign_count=verification.sign_count,
            transports=list(verification.transports) or None,
        )
        try:
            persisted = await self._credential_repository.create(credential)
        except IntegrityError as error:
            raise ApprovalConflictError(
                "This passkey credential is already registered",
                "PASSKEY_CREDENTIAL_ALREADY_REGISTERED",
            ) from error
        return PasskeyRegistrationResponse(
            credential=self._credential_response(persisted),
        )

    async def _get_identity(self, identity_id: str) -> ApprovalIdentity:
        identity = await self._identity_repository.get(identity_id)
        if identity is None:
            raise ApprovalNotFoundError(
                f"Approval identity '{identity_id}' was not found",
                "APPROVAL_IDENTITY_NOT_FOUND",
            )
        return identity

    async def _get_active_identity(self, identity_id: str) -> ApprovalIdentity:
        identity = await self._get_identity(identity_id)
        if ApprovalIdentityStatus(identity.status) is not ApprovalIdentityStatus.ACTIVE:
            raise ApprovalConflictError(
                f"Approval identity '{identity_id}' is disabled",
                "APPROVAL_IDENTITY_DISABLED",
            )
        return identity

    async def _identity_response(
        self,
        identity: ApprovalIdentity,
    ) -> ApprovalIdentityResponse:
        credentials = await self._credential_repository.list_for_identity(identity.id)
        return ApprovalIdentityResponse(
            id=identity.id,
            subject_ref=identity.subject_ref,
            display_name=identity.display_name,
            status=identity.status,
            credential_count=len(credentials),
            created_at=identity.created_at,
            updated_at=identity.updated_at,
        )

    @staticmethod
    def _credential_response(credential: PasskeyCredential) -> PasskeyCredentialResponse:
        return PasskeyCredentialResponse(
            id=credential.id,
            transports=credential.transports,
            created_at=credential.created_at,
            last_used_at=credential.last_used_at,
        )

    @property
    def _challenge_ttl_seconds(self) -> int:
        return int(self._challenge_ttl.total_seconds())

    def _read_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Passkey service clock must be timezone-aware")
        return value.astimezone(UTC)
