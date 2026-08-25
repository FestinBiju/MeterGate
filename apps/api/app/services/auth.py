"""Passkey-first account signup, login, and opaque-session orchestration."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Never

from sqlalchemy.exc import IntegrityError

from app.cache.auth import (
    AuthSessionRevokedError,
    AuthSessionState,
    AuthStateAlreadyExistsError,
    AuthStateAlreadyUsedError,
    AuthStateExpiredError,
    AuthStateInvalidError,
    AuthStateNotFoundError,
    AuthStore,
    LoginChallengeState,
    SignupChallengeState,
)
from app.domain.base64url import decode_base64url, encode_base64url
from app.domain.enums import AccountStatus, ApprovalIdentityStatus
from app.domain.exceptions import (
    AuthenticationConflictError,
    AuthenticationExpiredError,
    AuthenticationIntegrityError,
    AuthenticationNotFoundError,
    AuthenticationUnauthorizedError,
    AuthenticationVerificationError,
)
from app.domain.ids import (
    new_account_id,
    new_approval_challenge_id,
    new_approval_identity_id,
)
from app.models import Account, ApprovalIdentity, PasskeyCredential
from app.repositories.accounts import AccountRepository
from app.repositories.approval_identities import ApprovalIdentityRepository
from app.repositories.passkey_credentials import PasskeyCredentialRepository
from app.schemas.auth import (
    AccountResponse,
    AuthCeremonyOptionsResponse,
    AuthCeremonyVerify,
    AuthLogoutResponse,
    AuthPasskeyListResponse,
    AuthPasskeyResponse,
    AuthSessionResponse,
    SessionApprovalIdentityResponse,
    SignupOptionsCreate,
)
from app.services.webauthn import WebAuthnBackend, WebAuthnVerificationError

Clock = Callable[[], datetime]
logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class IssuedAuthSession:
    """A newly issued session, including the cookie value kept out of API schemas."""

    session_id: str
    response: AuthSessionResponse


@dataclass(frozen=True, slots=True)
class ResolvedAuthSession:
    """Internal authenticated principal resolved from Redis and PostgreSQL."""

    state: AuthSessionState
    account: Account
    approval_identity: ApprovalIdentity
    current_credential: PasskeyCredential
    response: AuthSessionResponse


class AuthenticationApplicationService:
    """Establish and resolve the authenticated buyer boundary using passkeys."""

    def __init__(
        self,
        account_repository: AccountRepository,
        identity_repository: ApprovalIdentityRepository,
        credential_repository: PasskeyCredentialRepository,
        auth_store: AuthStore,
        webauthn_backend: WebAuthnBackend,
        *,
        challenge_ttl: timedelta,
        session_ttl: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        if not timedelta(seconds=1) <= challenge_ttl <= timedelta(minutes=10):
            raise ValueError("Authentication challenge lifetime must be 1-600 seconds")
        if not timedelta(seconds=1) <= session_ttl <= timedelta(days=1):
            raise ValueError("Authentication session lifetime must be 1-86400 seconds")
        self._account_repository = account_repository
        self._identity_repository = identity_repository
        self._credential_repository = credential_repository
        self._auth_store = auth_store
        self._webauthn_backend = webauthn_backend
        self._challenge_ttl = challenge_ttl
        self._session_ttl = session_ttl
        self._clock = clock

    async def signup_options(
        self,
        payload: SignupOptionsCreate,
    ) -> AuthCeremonyOptionsResponse:
        """Create Redis-only first-passkey state without a half-created DB account."""
        account_id = new_account_id()
        identity_id = new_approval_identity_id()
        user_handle = secrets.token_bytes(32)
        return await self._create_challenge_options(
            kind="signup",
            display_name=payload.display_name,
            account_id=account_id,
            identity_id=identity_id,
            user_handle=user_handle,
        )

    async def signup_verify(self, payload: AuthCeremonyVerify) -> IssuedAuthSession:
        """Verify the first passkey, atomically create durable state, and sign in."""
        state = await self._consume_signup(payload.challenge_id)
        challenge = self._decode_challenge(
            state.challenge,
            reason_code="AUTH_SIGNUP_CHALLENGE_STATE_INVALID",
        )
        user_handle = self._decode_user_handle(state.user_handle)
        raw_credential_id = self._decode_credential_id(
            payload,
            reason_code="AUTH_SIGNUP_VERIFICATION_FAILED",
        )
        try:
            verification = self._webauthn_backend.verify_registration(
                credential=payload.credential.model_dump(exclude_none=True),
                expected_challenge=challenge,
            )
        except (ValueError, WebAuthnVerificationError) as error:
            raise AuthenticationVerificationError(
                "Passkey signup could not be verified",
                "AUTH_SIGNUP_VERIFICATION_FAILED",
            ) from error
        if not verification.user_verified or not compare_digest(
            verification.credential_id, raw_credential_id
        ):
            raise AuthenticationVerificationError(
                "Passkey signup could not be verified",
                "AUTH_SIGNUP_VERIFICATION_FAILED",
            )

        account = Account(
            id=state.account_id,
            display_name=state.display_name,
            status=AccountStatus.ACTIVE,
            session_version=1,
        )
        identity = ApprovalIdentity(
            id=state.approval_identity_id,
            account_id=state.account_id,
            subject_ref=state.account_id,
            display_name=state.display_name,
            webauthn_user_handle=user_handle,
            status=ApprovalIdentityStatus.ACTIVE,
        )
        credential = PasskeyCredential(
            approval_identity_id=identity.id,
            credential_id=verification.credential_id,
            public_key=verification.public_key,
            sign_count=verification.sign_count,
            transports=list(verification.transports) or None,
        )
        try:
            await self._account_repository.create_signup_bundle(
                account,
                identity=identity,
                credential=credential,
            )
        except IntegrityError as error:
            raise AuthenticationConflictError(
                "Passkey signup conflicts with existing account state",
                "AUTH_SIGNUP_VERIFICATION_FAILED",
            ) from error

        logger.info(
            "ACCOUNT_CREATED account_id=%s approval_identity_id=%s",
            account.id,
            identity.id,
        )
        return await self._issue_session(account, identity, credential)

    async def login_options(self) -> AuthCeremonyOptionsResponse:
        """Create identity-free options for a discoverable resident credential."""
        return await self._create_challenge_options(kind="login")

    async def login_verify(self, payload: AuthCeremonyVerify) -> IssuedAuthSession:
        """Resolve the asserted credential server-side and issue an opaque session."""
        state = await self._consume_login(payload.challenge_id)
        challenge = self._decode_challenge(
            state.challenge,
            reason_code="AUTH_LOGIN_CHALLENGE_STATE_INVALID",
        )
        raw_credential_id = self._decode_credential_id(
            payload,
            reason_code="AUTH_LOGIN_VERIFICATION_FAILED",
        )

        discovered_credential = await self._credential_repository.get_by_credential_id(
            raw_credential_id
        )
        if discovered_credential is None:
            self._raise_login_verification_failure()
        discovered_identity = await self._identity_repository.get(
            discovered_credential.approval_identity_id
        )
        if discovered_identity is None:
            self._raise_login_verification_failure()

        account = await self._account_repository.get_for_update(discovered_identity.account_id)
        if account is None:
            self._raise_login_verification_failure()
        if AccountStatus(account.status) is AccountStatus.PENDING:
            self._raise_login_verification_failure()
        if AccountStatus(account.status) is AccountStatus.DISABLED:
            logger.info(
                "PASSKEY_LOGIN_FAILED account_id=%s reason_code=AUTH_ACCOUNT_DISABLED", account.id
            )
            raise AuthenticationUnauthorizedError(
                "Account is disabled",
                "AUTH_ACCOUNT_DISABLED",
            )

        identity = await self._identity_repository.get_for_update(discovered_identity.id)
        if (
            identity is None
            or identity.account_id != account.id
            or ApprovalIdentityStatus(identity.status) is not ApprovalIdentityStatus.ACTIVE
        ):
            self._raise_login_verification_failure()
        credential = await self._credential_repository.get_by_credential_id_for_update(
            raw_credential_id,
            identity_id=identity.id,
        )
        if credential is None:
            self._raise_login_verification_failure()
        self._require_login_user_handle(payload, identity)

        try:
            verification = self._webauthn_backend.verify_authentication(
                credential=payload.credential.model_dump(exclude_none=True),
                expected_challenge=challenge,
                credential_public_key=credential.public_key,
                credential_current_sign_count=credential.sign_count,
            )
        except WebAuthnVerificationError as error:
            self._raise_login_verification_failure(error)
        if (
            not verification.user_verified
            or not compare_digest(verification.credential_id, credential.credential_id)
            or verification.new_sign_count < credential.sign_count
        ):
            self._raise_login_verification_failure()

        authenticated_at = self._read_clock()
        try:
            credential = await self._credential_repository.update_locked_usage(
                credential,
                new_sign_count=verification.new_sign_count,
                last_used_at=authenticated_at,
            )
        except ValueError as error:
            self._raise_login_verification_failure(error)

        logger.info(
            "PASSKEY_LOGIN_SUCCESS account_id=%s approval_identity_id=%s credential_id=%s",
            account.id,
            identity.id,
            credential.id,
        )
        return await self._issue_session(
            account,
            identity,
            credential,
            authenticated_at=authenticated_at,
        )

    async def resolve_session(self, session_id: str) -> ResolvedAuthSession:
        """Resolve one opaque Redis session into a currently active DB principal."""
        try:
            state = await self._auth_store.get_session(session_id)
        except AuthStateExpiredError as error:
            raise AuthenticationUnauthorizedError(
                "Authentication session has expired",
                "AUTH_SESSION_EXPIRED",
            ) from error
        except (AuthStateNotFoundError, AuthSessionRevokedError) as error:
            raise AuthenticationUnauthorizedError(
                "Authentication session is invalid",
                "AUTH_SESSION_INVALID",
            ) from error
        except AuthStateInvalidError as error:
            raise AuthenticationUnauthorizedError(
                "Authentication session is invalid",
                "AUTH_SESSION_INVALID",
            ) from error

        now = self._read_clock()
        if now < state.created_at or now < state.authenticated_at:
            raise AuthenticationUnauthorizedError(
                "Authentication session is invalid",
                "AUTH_SESSION_INVALID",
            )
        if now >= state.expires_at:
            raise AuthenticationUnauthorizedError(
                "Authentication session has expired",
                "AUTH_SESSION_EXPIRED",
            )

        account = await self._account_repository.get(state.account_id)
        if account is None or account.session_version != state.account_session_version:
            raise AuthenticationUnauthorizedError(
                "Authentication session is invalid",
                "AUTH_SESSION_INVALID",
            )
        account_status = AccountStatus(account.status)
        if account_status is AccountStatus.PENDING:
            raise AuthenticationUnauthorizedError(
                "Account is pending first-passkey verification",
                "AUTH_ACCOUNT_PENDING",
            )
        if account_status is AccountStatus.DISABLED:
            raise AuthenticationUnauthorizedError(
                "Account is disabled",
                "AUTH_ACCOUNT_DISABLED",
            )

        identity = await self._identity_repository.get_by_account_id(account.id)
        credential = await self._credential_repository.get(state.passkey_credential_id)
        if (
            identity is None
            or identity.id != state.approval_identity_id
            or ApprovalIdentityStatus(identity.status) is not ApprovalIdentityStatus.ACTIVE
            or credential is None
            or credential.approval_identity_id != identity.id
        ):
            raise AuthenticationUnauthorizedError(
                "Authentication session is invalid",
                "AUTH_SESSION_INVALID",
            )

        credentials = await self._credential_repository.list_for_identity(identity.id)
        response = self._session_response(state, account, identity, credentials)
        return ResolvedAuthSession(
            state=state,
            account=account,
            approval_identity=identity,
            current_credential=credential,
            response=response,
        )

    async def get_session(self, session_id: str) -> AuthSessionResponse:
        """Return only the safe browser session projection."""
        return (await self.resolve_session(session_id)).response

    async def logout(self, session_id: str) -> AuthLogoutResponse:
        """Revoke the current server-side session; no browser token remains sufficient."""
        try:
            state = await self._auth_store.get_session(session_id)
            await self._auth_store.revoke_session(session_id)
        except AuthStateExpiredError as error:
            raise AuthenticationUnauthorizedError(
                "Authentication session has expired",
                "AUTH_SESSION_EXPIRED",
            ) from error
        except (AuthStateNotFoundError, AuthSessionRevokedError, AuthStateInvalidError) as error:
            raise AuthenticationUnauthorizedError(
                "Authentication session is invalid",
                "AUTH_SESSION_INVALID",
            ) from error
        logger.info("SESSION_REVOKED account_id=%s", state.account_id)
        return AuthLogoutResponse()

    async def list_passkeys(
        self,
        resolved: ResolvedAuthSession,
    ) -> AuthPasskeyListResponse:
        """List safe metadata for credentials owned by the resolved account."""
        credentials = await self._credential_repository.list_for_identity(
            resolved.approval_identity.id
        )
        return AuthPasskeyListResponse(
            credentials=[
                AuthPasskeyResponse(
                    id=credential.id,
                    transports=credential.transports,
                    created_at=credential.created_at,
                    last_used_at=credential.last_used_at,
                    current=credential.id == resolved.state.passkey_credential_id,
                )
                for credential in credentials
            ]
        )

    async def _create_challenge_options(
        self,
        *,
        kind: str,
        display_name: str | None = None,
        account_id: str | None = None,
        identity_id: str | None = None,
        user_handle: bytes | None = None,
    ) -> AuthCeremonyOptionsResponse:
        for _ in range(3):
            issued_at = self._read_clock()
            expires_at = issued_at + self._challenge_ttl
            challenge_id = new_approval_challenge_id()
            challenge = secrets.token_bytes(32)
            if kind == "signup":
                if None in (display_name, account_id, identity_id, user_handle):
                    raise ValueError("Signup challenge metadata is incomplete")
                assert isinstance(display_name, str)
                assert isinstance(account_id, str)
                assert isinstance(identity_id, str)
                assert isinstance(user_handle, bytes)
                state: SignupChallengeState | LoginChallengeState = SignupChallengeState(
                    challenge_id=challenge_id,
                    challenge=encode_base64url(challenge),
                    account_id=account_id,
                    approval_identity_id=identity_id,
                    user_handle=encode_base64url(user_handle),
                    display_name=display_name,
                    issued_at=issued_at,
                    expires_at=expires_at,
                )
                public_key = self._webauthn_backend.registration_options(
                    challenge=challenge,
                    user_handle=user_handle,
                    user_name=account_id,
                    user_display_name=display_name,
                )
                save = self._auth_store.save_signup
                creation_code = "AUTH_SIGNUP_CHALLENGE_CREATION_FAILED"
            elif kind == "login":
                state = LoginChallengeState(
                    challenge_id=challenge_id,
                    challenge=encode_base64url(challenge),
                    issued_at=issued_at,
                    expires_at=expires_at,
                )
                public_key = self._webauthn_backend.authentication_options(
                    challenge=challenge,
                    allow_credentials=None,
                )
                save = self._auth_store.save_login
                creation_code = "AUTH_LOGIN_CHALLENGE_CREATION_FAILED"
            else:
                raise ValueError("Unsupported authentication challenge kind")

            if self._read_clock() >= expires_at:
                raise AuthenticationIntegrityError(
                    "Authentication challenge expired during option generation",
                    creation_code,
                )
            try:
                await save(state, ttl_seconds=self._challenge_ttl_seconds)  # type: ignore[arg-type]
            except AuthStateAlreadyExistsError:
                continue
            return AuthCeremonyOptionsResponse(
                challenge_id=challenge_id,
                public_key=public_key,
                expires_at=expires_at,
            )
        raise AuthenticationIntegrityError(
            "A unique authentication challenge could not be created",
            creation_code,
        )

    async def _issue_session(
        self,
        account: Account,
        identity: ApprovalIdentity,
        credential: PasskeyCredential,
        *,
        authenticated_at: datetime | None = None,
    ) -> IssuedAuthSession:
        issued_at = authenticated_at or self._read_clock()
        issued_at = self._as_utc(issued_at)
        credentials = await self._credential_repository.list_for_identity(identity.id)
        for _ in range(3):
            session_id = f"ses_{encode_base64url(secrets.token_bytes(32))}"
            state = AuthSessionState(
                session_id=session_id,
                account_id=account.id,
                approval_identity_id=identity.id,
                passkey_credential_id=credential.id,
                account_session_version=account.session_version,
                csrf_token=encode_base64url(secrets.token_bytes(32)),
                created_at=issued_at,
                authenticated_at=issued_at,
                expires_at=issued_at + self._session_ttl,
            )
            if self._read_clock() >= state.expires_at:
                break
            try:
                await self._auth_store.save_session(
                    state,
                    ttl_seconds=self._session_ttl_seconds,
                )
            except AuthStateAlreadyExistsError:
                continue
            except (AuthStateInvalidError, AuthStateExpiredError) as error:
                raise AuthenticationIntegrityError(
                    "Authentication session could not be created",
                    "AUTH_SESSION_CREATION_FAILED",
                ) from error
            logger.info(
                "SESSION_CREATED account_id=%s approval_identity_id=%s credential_id=%s",
                account.id,
                identity.id,
                credential.id,
            )
            return IssuedAuthSession(
                session_id=session_id,
                response=self._session_response(state, account, identity, credentials),
            )
        raise AuthenticationIntegrityError(
            "Authentication session could not be created",
            "AUTH_SESSION_CREATION_FAILED",
        )

    @staticmethod
    def _session_response(
        state: AuthSessionState,
        account: Account,
        identity: ApprovalIdentity,
        credentials: list[PasskeyCredential],
    ) -> AuthSessionResponse:
        return AuthSessionResponse(
            account=AccountResponse(
                id=account.id,
                display_name=account.display_name,
                status=account.status,
                created_at=account.created_at,
                updated_at=account.updated_at,
            ),
            approval_identity=SessionApprovalIdentityResponse(
                id=identity.id,
                status=identity.status,
                credential_count=len(credentials),
            ),
            authenticated_at=state.authenticated_at,
            expires_at=state.expires_at,
            csrf_token=state.csrf_token,
        )

    async def _consume_signup(self, challenge_id: str) -> SignupChallengeState:
        try:
            return await self._auth_store.consume_signup(challenge_id)
        except AuthStateNotFoundError as error:
            raise AuthenticationNotFoundError(
                str(error),
                "AUTH_SIGNUP_CHALLENGE_NOT_FOUND",
            ) from error
        except AuthStateExpiredError as error:
            raise AuthenticationExpiredError(
                str(error),
                "AUTH_SIGNUP_CHALLENGE_EXPIRED",
            ) from error
        except AuthStateAlreadyUsedError as error:
            raise AuthenticationConflictError(
                str(error),
                "AUTH_SIGNUP_CHALLENGE_ALREADY_USED",
            ) from error
        except AuthStateInvalidError as error:
            raise AuthenticationIntegrityError(
                str(error),
                "AUTH_SIGNUP_CHALLENGE_STATE_INVALID",
            ) from error

    async def _consume_login(self, challenge_id: str) -> LoginChallengeState:
        try:
            return await self._auth_store.consume_login(challenge_id)
        except AuthStateNotFoundError as error:
            raise AuthenticationNotFoundError(
                str(error),
                "AUTH_LOGIN_CHALLENGE_NOT_FOUND",
            ) from error
        except AuthStateExpiredError as error:
            raise AuthenticationExpiredError(
                str(error),
                "AUTH_LOGIN_CHALLENGE_EXPIRED",
            ) from error
        except AuthStateAlreadyUsedError as error:
            raise AuthenticationConflictError(
                str(error),
                "AUTH_LOGIN_CHALLENGE_ALREADY_USED",
            ) from error
        except AuthStateInvalidError as error:
            raise AuthenticationIntegrityError(
                str(error),
                "AUTH_LOGIN_CHALLENGE_STATE_INVALID",
            ) from error

    @staticmethod
    def _decode_challenge(value: str, *, reason_code: str) -> bytes:
        try:
            return decode_base64url(value, maximum_bytes=96)
        except ValueError as error:
            raise AuthenticationIntegrityError(
                "Stored authentication challenge is invalid",
                reason_code,
            ) from error

    @staticmethod
    def _decode_user_handle(value: str) -> bytes:
        try:
            handle = decode_base64url(value, maximum_bytes=64)
        except ValueError as error:
            raise AuthenticationIntegrityError(
                "Stored signup identity binding is invalid",
                "AUTH_SIGNUP_CHALLENGE_STATE_INVALID",
            ) from error
        if not 1 <= len(handle) <= 64:
            raise AuthenticationIntegrityError(
                "Stored signup identity binding is invalid",
                "AUTH_SIGNUP_CHALLENGE_STATE_INVALID",
            )
        return handle

    @staticmethod
    def _decode_credential_id(payload: AuthCeremonyVerify, *, reason_code: str) -> bytes:
        try:
            raw_id = decode_base64url(payload.credential.rawId, maximum_bytes=1_024)
            id_value = decode_base64url(payload.credential.id, maximum_bytes=1_024)
        except ValueError as error:
            raise AuthenticationVerificationError(
                "Passkey response is malformed",
                reason_code,
            ) from error
        if not raw_id or not compare_digest(raw_id, id_value):
            raise AuthenticationVerificationError(
                "Passkey response is malformed",
                reason_code,
            )
        return raw_id

    @staticmethod
    def _require_login_user_handle(
        payload: AuthCeremonyVerify,
        identity: ApprovalIdentity,
    ) -> None:
        encoded = payload.credential.response.get("userHandle")
        if not isinstance(encoded, str):
            AuthenticationApplicationService._raise_login_verification_failure()
        try:
            user_handle = decode_base64url(encoded, maximum_bytes=64)
        except ValueError as error:
            AuthenticationApplicationService._raise_login_verification_failure(error)
        if not compare_digest(user_handle, identity.webauthn_user_handle):
            AuthenticationApplicationService._raise_login_verification_failure()

    @staticmethod
    def _raise_login_verification_failure(error: Exception | None = None) -> Never:
        logger.info("PASSKEY_LOGIN_FAILED reason_code=AUTH_LOGIN_VERIFICATION_FAILED")
        failure = AuthenticationVerificationError(
            "Passkey login could not be verified",
            "AUTH_LOGIN_VERIFICATION_FAILED",
        )
        if error is None:
            raise failure
        raise failure from error

    @property
    def _challenge_ttl_seconds(self) -> int:
        return int(self._challenge_ttl.total_seconds())

    @property
    def _session_ttl_seconds(self) -> int:
        return int(self._session_ttl.total_seconds())

    def _read_clock(self) -> datetime:
        return self._as_utc(self._clock())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Authentication timestamps must be timezone-aware")
        return value.astimezone(UTC)
