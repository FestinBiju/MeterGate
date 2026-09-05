"""Passkey-backed, action-bound proof-of-human-presence orchestration."""

import secrets
from datetime import UTC, datetime, timedelta
from hmac import compare_digest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.approval_challenges import AllowedCredentialBinding, ChallengeStoreError
from app.cache.human_presence import HumanPresenceChallengeState, HumanPresenceStore
from app.core.config import Settings
from app.domain.base64url import decode_base64url, encode_base64url
from app.domain.enums import AccountStatus
from app.domain.exceptions import (
    AuthenticationConflictError,
    AuthenticationExpiredError,
    AuthenticationForbiddenError,
    AuthenticationVerificationError,
)
from app.domain.hashing import sha256_bytes
from app.domain.human_presence import (
    HUMAN_PRESENCE_VERSION,
    HumanPresenceAction,
    calculate_presence_hash,
    normalize_resource_binding,
    resource_binding_hash,
)
from app.domain.ids import new_human_presence_challenge_id, new_human_presence_proof_id
from app.models import Account, ApprovalIdentity, HumanPresenceProof, PasskeyCredential
from app.schemas.human_presence import (
    HumanPresenceChallengeCreate,
    HumanPresenceChallengeResponse,
    HumanPresenceProofResponse,
    HumanPresenceStatusResponse,
    HumanPresenceVerify,
)
from app.services.webauthn import (
    WebAuthnBackend,
    WebAuthnCredentialDescriptor,
    WebAuthnVerificationError,
)


class HumanPresenceService:
    def __init__(
        self,
        session: AsyncSession,
        store: HumanPresenceStore,
        webauthn: WebAuthnBackend,
        settings: Settings,
    ) -> None:
        self._session = session
        self._store = store
        self._webauthn = webauthn
        self._settings = settings

    async def create_challenge(
        self,
        payload: HumanPresenceChallengeCreate,
        *,
        account_id: str,
        session_id: str,
        origin: str,
    ) -> HumanPresenceChallengeResponse:
        binding = normalize_resource_binding(payload.action_class, dict(payload.resource_binding))
        identity = await self._active_identity(account_id)
        credentials = list(
            (
                await self._session.scalars(
                    select(PasskeyCredential)
                    .where(PasskeyCredential.approval_identity_id == identity.id)
                    .order_by(PasskeyCredential.id)
                )
            ).all()
        )
        if not credentials:
            raise AuthenticationForbiddenError(
                "A registered passkey is required", "HUMAN_PRESENCE_PASSKEY_REQUIRED"
            )
        challenge = secrets.token_bytes(32)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._settings.human_presence_challenge_ttl_seconds)
        challenge_id = new_human_presence_challenge_id()
        descriptors = [
            WebAuthnCredentialDescriptor(
                credential.credential_id, tuple(credential.transports or ())
            )
            for credential in credentials
        ]
        public_key = self._webauthn.authentication_options(
            challenge=challenge, allow_credentials=descriptors
        )
        state = HumanPresenceChallengeState(
            challenge_id=challenge_id,
            challenge=encode_base64url(challenge),
            account_id=account_id,
            session_id_hash=sha256_bytes(session_id.encode()),
            action_class=payload.action_class,
            resource_binding=binding,
            resource_binding_hash=resource_binding_hash(payload.action_class, binding),
            origin=origin,
            approval_identity_id=identity.id,
            allowed_credentials=[
                AllowedCredentialBinding(
                    passkey_credential_id=item.id,
                    credential_id_hash=sha256_bytes(item.credential_id),
                )
                for item in credentials
            ],
            issued_at=now,
            expires_at=expires_at,
        )
        await self._store.save(
            state, ttl_seconds=self._settings.human_presence_challenge_ttl_seconds
        )
        return HumanPresenceChallengeResponse(
            challenge_id=challenge_id,
            action_class=payload.action_class,
            resource_binding=binding,
            public_key=public_key,
            expires_at=expires_at,
        )

    async def verify(
        self,
        challenge_id: str,
        payload: HumanPresenceVerify,
        *,
        account_id: str,
        session_id: str,
        origin: str,
        account_session_version: int,
    ) -> HumanPresenceProofResponse:
        try:
            state = await self._store.consume(challenge_id, account_id=account_id)
        except ChallengeStoreError as error:
            raise AuthenticationConflictError(
                "Human-presence challenge is unavailable", "HUMAN_PRESENCE_CHALLENGE_INVALID"
            ) from error
        if state.origin != origin or state.session_id_hash != sha256_bytes(session_id.encode()):
            raise AuthenticationForbiddenError(
                "Human-presence challenge binding does not match", "HUMAN_PRESENCE_BINDING_MISMATCH"
            )
        account = await self._session.scalar(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        if (
            account is None
            or AccountStatus(account.status) is not AccountStatus.ACTIVE
            or account.session_version != account_session_version
        ):
            raise AuthenticationForbiddenError(
                "The account is not active", "HUMAN_PRESENCE_ACCOUNT_INVALID"
            )
        try:
            raw_id = decode_base64url(payload.credential.rawId, maximum_bytes=1_024)
        except ValueError as error:
            raise AuthenticationVerificationError(
                "Human-presence assertion is malformed", "HUMAN_PRESENCE_VERIFICATION_FAILED"
            ) from error
        credential = await self._session.scalar(
            select(PasskeyCredential)
            .where(
                PasskeyCredential.credential_id == raw_id,
                PasskeyCredential.approval_identity_id == state.approval_identity_id,
            )
            .with_for_update()
        )
        allowed = next(
            (
                item
                for item in state.allowed_credentials
                if item.passkey_credential_id == getattr(credential, "id", None)
            ),
            None,
        )
        if (
            credential is None
            or allowed is None
            or not compare_digest(
                allowed.credential_id_hash, sha256_bytes(credential.credential_id)
            )
        ):
            raise AuthenticationForbiddenError(
                "Passkey was not allowed for this challenge", "HUMAN_PRESENCE_CREDENTIAL_INVALID"
            )
        try:
            verified = self._webauthn.verify_authentication(
                credential=payload.credential.model_dump(exclude_none=True),
                expected_challenge=decode_base64url(state.challenge, maximum_bytes=96),
                credential_public_key=credential.public_key,
                credential_current_sign_count=credential.sign_count,
            )
        except WebAuthnVerificationError as error:
            raise AuthenticationVerificationError(
                "Human presence could not be verified", "HUMAN_PRESENCE_VERIFICATION_FAILED"
            ) from error
        if not verified.user_verified or not compare_digest(
            verified.credential_id, credential.credential_id
        ):
            raise AuthenticationVerificationError(
                "Authenticator user verification is required",
                "HUMAN_PRESENCE_USER_VERIFICATION_REQUIRED",
            )
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._settings.human_presence_ttl_seconds)
        proof_id = new_human_presence_proof_id()
        challenge_hash = sha256_bytes(decode_base64url(state.challenge, maximum_bytes=96))
        proof = HumanPresenceProof(
            id=proof_id,
            account_id=account_id,
            session_id_hash=state.session_id_hash,
            passkey_credential_id=credential.id,
            action_class=state.action_class,
            resource_binding=state.resource_binding,
            origin=origin,
            challenge_hash=challenge_hash,
            presence_version=HUMAN_PRESENCE_VERSION,
            presence_hash=calculate_presence_hash(
                proof_id=proof_id,
                account_id=account_id,
                session_id_hash=state.session_id_hash,
                passkey_credential_id=credential.id,
                action_class=state.action_class,
                resource_binding=state.resource_binding,
                origin=origin,
                issued_at=now,
                expires_at=expires_at,
                challenge_hash=challenge_hash,
            ),
            issued_at=now,
            expires_at=expires_at,
        )
        credential.sign_count = verified.new_sign_count
        credential.last_used_at = now
        self._session.add(proof)
        await self._session.commit()
        return self._response(proof)

    async def status(self, *, account_id: str, session_id: str) -> HumanPresenceStatusResponse:
        session_hash = sha256_bytes(session_id.encode())
        rows = list(
            (
                await self._session.scalars(
                    select(HumanPresenceProof)
                    .where(
                        HumanPresenceProof.account_id == account_id,
                        HumanPresenceProof.session_id_hash == session_hash,
                    )
                    .order_by(HumanPresenceProof.issued_at.desc())
                    .limit(20)
                )
            ).all()
        )
        return HumanPresenceStatusResponse(proofs=[self._response(row) for row in rows])

    async def require_and_consume(
        self,
        proof_id: str,
        *,
        account_id: str,
        session_id: str,
        origin: str,
        action_class: HumanPresenceAction,
        resource_binding: dict[str, object],
    ) -> None:
        proof = await self._session.scalar(
            select(HumanPresenceProof)
            .where(HumanPresenceProof.id == proof_id, HumanPresenceProof.account_id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = datetime.now(UTC)
        if proof is None:
            raise AuthenticationForbiddenError(
                "Human presence is required", "HUMAN_PRESENCE_REQUIRED"
            )
        expires = (
            proof.expires_at if proof.expires_at.tzinfo else proof.expires_at.replace(tzinfo=UTC)
        )
        expected_binding = normalize_resource_binding(action_class, resource_binding)
        if proof.account_id != account_id:
            raise AuthenticationForbiddenError(
                "Human-presence proof has the wrong owner",
                "HUMAN_PRESENCE_SCOPE_MISMATCH",
            )
        if proof.consumed_at is not None:
            raise AuthenticationConflictError(
                "Human-presence proof was already used", "HUMAN_PRESENCE_PROOF_REPLAYED"
            )
        if expires <= now:
            raise AuthenticationExpiredError(
                "Human-presence proof expired", "HUMAN_PRESENCE_PROOF_EXPIRED"
            )
        if (
            proof.session_id_hash != sha256_bytes(session_id.encode())
            or proof.origin != origin
            or proof.action_class != action_class
            or proof.resource_binding != expected_binding
        ):
            raise AuthenticationForbiddenError(
                "Human-presence proof has the wrong scope", "HUMAN_PRESENCE_SCOPE_MISMATCH"
            )
        expected_hash = calculate_presence_hash(
            proof_id=proof.id,
            account_id=proof.account_id,
            session_id_hash=proof.session_id_hash,
            passkey_credential_id=proof.passkey_credential_id,
            action_class=action_class,
            resource_binding=proof.resource_binding,
            origin=proof.origin,
            issued_at=proof.issued_at,
            expires_at=proof.expires_at,
            challenge_hash=proof.challenge_hash,
        )
        if not compare_digest(expected_hash, proof.presence_hash):
            raise AuthenticationForbiddenError(
                "Human-presence proof integrity failed", "HUMAN_PRESENCE_INTEGRITY_FAILED"
            )
        proof.consumed_at = now

    async def _active_identity(self, account_id: str) -> ApprovalIdentity:
        identity = await self._session.scalar(
            select(ApprovalIdentity).where(ApprovalIdentity.account_id == account_id)
        )
        if identity is None or identity.status != "active":
            raise AuthenticationForbiddenError(
                "An active approval identity is required", "HUMAN_PRESENCE_IDENTITY_REQUIRED"
            )
        return identity

    @staticmethod
    def _response(proof: HumanPresenceProof) -> HumanPresenceProofResponse:
        now = datetime.now(UTC)
        expires = (
            proof.expires_at if proof.expires_at.tzinfo else proof.expires_at.replace(tzinfo=UTC)
        )
        state = (
            "consumed"
            if proof.consumed_at is not None
            else "expired"
            if expires <= now
            else "active"
        )
        return HumanPresenceProofResponse(
            id=proof.id,
            action_class=proof.action_class,
            resource_binding=proof.resource_binding,
            issued_at=proof.issued_at,
            expires_at=proof.expires_at,
            state=state,
            presence_version=proof.presence_version,
            presence_hash=proof.presence_hash,
        )
