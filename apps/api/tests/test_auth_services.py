from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.cache.auth import (
    AuthSessionRevokedError,
    AuthSessionState,
    AuthStateAlreadyExistsError,
    AuthStateAlreadyUsedError,
    AuthStateExpiredError,
    AuthStateInvalidError,
    AuthStateNotFoundError,
    LoginChallengeState,
    SignupChallengeState,
)
from app.domain.base64url import encode_base64url
from app.domain.enums import AccountStatus, ApprovalIdentityStatus
from app.domain.exceptions import (
    AuthenticationConflictError,
    AuthenticationExpiredError,
    AuthenticationUnauthorizedError,
    AuthenticationVerificationError,
)
from app.models import Account, ApprovalIdentity, PasskeyCredential
from app.schemas.approvals import BrowserCredential
from app.schemas.auth import AuthCeremonyVerify, SignupOptionsCreate
from app.services.auth import AuthenticationApplicationService
from app.services.webauthn import (
    AuthenticationVerification,
    PyWebAuthnBackend,
    RegistrationVerification,
    WebAuthnCredentialDescriptor,
    WebAuthnVerificationError,
)

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
ACCOUNT_ID = "acct_00000000000000000000000001"
IDENTITY_ID = "aid_00000000000000000000000001"
CREDENTIAL_ID = "pkc_00000000000000000000000001"
RAW_CREDENTIAL_ID = b"credential-id"
PUBLIC_KEY = b"public-key"
USER_HANDLE = b"u" * 32


@dataclass
class FrozenClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


class MemoryAccountRepository:
    def __init__(self, accounts: Sequence[Account] = ()) -> None:
        self.records = {account.id: account for account in accounts}
        self.bundle_error = False
        self.identity_records: dict[str, ApprovalIdentity] | None = None
        self.credential_records: dict[str, PasskeyCredential] | None = None

    async def get(self, account_id: str) -> Account | None:
        return self.records.get(account_id)

    async def get_for_update(self, account_id: str) -> Account | None:
        return self.records.get(account_id)

    async def create_signup_bundle(
        self,
        account: Account,
        *,
        identity: ApprovalIdentity,
        credential: PasskeyCredential,
    ) -> Account:
        if self.bundle_error:
            raise IntegrityError("signup", {}, Exception("conflict"))
        account.created_at = NOW
        account.updated_at = NOW
        identity.created_at = NOW
        identity.updated_at = NOW
        credential.id = CREDENTIAL_ID
        credential.created_at = NOW
        credential.last_used_at = None
        self.records[account.id] = account
        assert self.identity_records is not None
        assert self.credential_records is not None
        self.identity_records[identity.id] = identity
        self.credential_records[credential.id] = credential
        return account


class MemoryIdentityRepository:
    def __init__(self, identities: Sequence[ApprovalIdentity] = ()) -> None:
        self.records = {identity.id: identity for identity in identities}

    async def get(self, identity_id: str) -> ApprovalIdentity | None:
        return self.records.get(identity_id)

    async def get_for_update(self, identity_id: str) -> ApprovalIdentity | None:
        return self.records.get(identity_id)

    async def get_by_account_id(self, account_id: str) -> ApprovalIdentity | None:
        return next(
            (item for item in self.records.values() if item.account_id == account_id),
            None,
        )


class MemoryCredentialRepository:
    def __init__(self, credentials: Sequence[PasskeyCredential] = ()) -> None:
        self.records = {credential.id: credential for credential in credentials}
        self.usage_commits = 0

    async def get(self, credential_id: str) -> PasskeyCredential | None:
        return self.records.get(credential_id)

    async def get_by_credential_id(self, credential_id: bytes) -> PasskeyCredential | None:
        return next(
            (item for item in self.records.values() if item.credential_id == credential_id),
            None,
        )

    async def list_for_identity(self, identity_id: str) -> list[PasskeyCredential]:
        return [item for item in self.records.values() if item.approval_identity_id == identity_id]

    async def get_by_credential_id_for_update(
        self,
        credential_id: bytes,
        *,
        identity_id: str,
    ) -> PasskeyCredential | None:
        credential = await self.get_by_credential_id(credential_id)
        if credential is None or credential.approval_identity_id != identity_id:
            return None
        return credential

    async def update_locked_usage(
        self,
        credential: PasskeyCredential,
        *,
        new_sign_count: int,
        last_used_at: datetime,
    ) -> PasskeyCredential:
        if new_sign_count < credential.sign_count:
            raise ValueError("counter regression")
        credential.sign_count = new_sign_count
        credential.last_used_at = last_used_at
        self.usage_commits += 1
        return credential


class MemoryAuthStore:
    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.signups: dict[str, SignupChallengeState] = {}
        self.logins: dict[str, LoginChallengeState] = {}
        self.sessions: dict[str, AuthSessionState] = {}
        self.used_signups: set[str] = set()
        self.used_logins: set[str] = set()
        self.revoked_sessions: set[str] = set()
        self.saved_ttls: list[int] = []
        self.invalid_session = False

    async def save_signup(self, state: SignupChallengeState, *, ttl_seconds: int) -> None:
        if state.challenge_id in self.signups:
            raise AuthStateAlreadyExistsError(state.challenge_id)
        self.signups[state.challenge_id] = state
        self.saved_ttls.append(ttl_seconds)

    async def consume_signup(self, challenge_id: str) -> SignupChallengeState:
        if challenge_id in self.used_signups:
            raise AuthStateAlreadyUsedError(challenge_id)
        state = self.signups.pop(challenge_id, None)
        if state is None:
            raise AuthStateNotFoundError(challenge_id)
        self.used_signups.add(challenge_id)
        if self.clock.current >= state.expires_at:
            raise AuthStateExpiredError(challenge_id)
        return state

    async def save_login(self, state: LoginChallengeState, *, ttl_seconds: int) -> None:
        if state.challenge_id in self.logins:
            raise AuthStateAlreadyExistsError(state.challenge_id)
        self.logins[state.challenge_id] = state
        self.saved_ttls.append(ttl_seconds)

    async def consume_login(self, challenge_id: str) -> LoginChallengeState:
        if challenge_id in self.used_logins:
            raise AuthStateAlreadyUsedError(challenge_id)
        state = self.logins.pop(challenge_id, None)
        if state is None:
            raise AuthStateNotFoundError(challenge_id)
        self.used_logins.add(challenge_id)
        if self.clock.current >= state.expires_at:
            raise AuthStateExpiredError(challenge_id)
        return state

    async def save_session(self, state: AuthSessionState, *, ttl_seconds: int) -> None:
        if state.session_id in self.sessions:
            raise AuthStateAlreadyExistsError(state.session_id)
        self.sessions[state.session_id] = state
        self.saved_ttls.append(ttl_seconds)

    async def get_session(self, session_id: str) -> AuthSessionState:
        if self.invalid_session:
            raise AuthStateInvalidError(session_id)
        if session_id in self.revoked_sessions:
            raise AuthSessionRevokedError(session_id)
        state = self.sessions.get(session_id)
        if state is None:
            raise AuthStateNotFoundError(session_id)
        return state

    async def revoke_session(self, session_id: str) -> None:
        if session_id in self.revoked_sessions:
            raise AuthSessionRevokedError(session_id)
        if self.sessions.pop(session_id, None) is None:
            raise AuthStateNotFoundError(session_id)
        self.revoked_sessions.add(session_id)


class FakeWebAuthnBackend:
    def __init__(self) -> None:
        self.registration_verification = RegistrationVerification(
            credential_id=RAW_CREDENTIAL_ID,
            public_key=PUBLIC_KEY,
            sign_count=0,
            transports=("internal",),
            device_type="single_device",
            backed_up=False,
            user_verified=True,
        )
        self.authentication_verification = AuthenticationVerification(
            credential_id=RAW_CREDENTIAL_ID,
            new_sign_count=1,
            device_type="single_device",
            backed_up=False,
            user_verified=True,
        )
        self.registration_error = False
        self.authentication_error = False
        self.registration_user_handle: bytes | None = None
        self.authentication_allows: Sequence[WebAuthnCredentialDescriptor] | None | object = (
            object()
        )

    def registration_options(
        self,
        *,
        challenge: bytes,
        user_handle: bytes,
        user_name: str,
        user_display_name: str,
        exclude_credentials: Sequence[WebAuthnCredentialDescriptor] = (),
    ) -> dict[str, Any]:
        del challenge, user_name, user_display_name, exclude_credentials
        self.registration_user_handle = user_handle
        return {"challenge": "signup", "userVerification": "required"}

    def verify_registration(
        self,
        *,
        credential: Mapping[str, Any],
        expected_challenge: bytes,
    ) -> RegistrationVerification:
        del credential, expected_challenge
        if self.registration_error:
            raise WebAuthnVerificationError()
        return self.registration_verification

    def authentication_options(
        self,
        *,
        challenge: bytes,
        allow_credentials: Sequence[WebAuthnCredentialDescriptor] | None = None,
    ) -> dict[str, Any]:
        del challenge
        self.authentication_allows = allow_credentials
        return {"challenge": "login", "userVerification": "required"}

    def verify_authentication(
        self,
        *,
        credential: Mapping[str, Any],
        expected_challenge: bytes,
        credential_public_key: bytes,
        credential_current_sign_count: int,
    ) -> AuthenticationVerification:
        del credential, expected_challenge, credential_public_key, credential_current_sign_count
        if self.authentication_error:
            raise WebAuthnVerificationError()
        return self.authentication_verification


def make_account(*, status: AccountStatus = AccountStatus.ACTIVE) -> Account:
    return Account(
        id=ACCOUNT_ID,
        display_name="Test Buyer",
        status=status,
        session_version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def make_identity(
    *,
    status: ApprovalIdentityStatus = ApprovalIdentityStatus.ACTIVE,
) -> ApprovalIdentity:
    return ApprovalIdentity(
        id=IDENTITY_ID,
        account_id=ACCOUNT_ID,
        subject_ref=ACCOUNT_ID,
        display_name="Test Buyer",
        webauthn_user_handle=USER_HANDLE,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


def make_credential(*, sign_count: int = 0) -> PasskeyCredential:
    return PasskeyCredential(
        id=CREDENTIAL_ID,
        approval_identity_id=IDENTITY_ID,
        credential_id=RAW_CREDENTIAL_ID,
        public_key=PUBLIC_KEY,
        sign_count=sign_count,
        transports=["internal"],
        created_at=NOW,
        last_used_at=None,
    )


def browser_credential(
    *,
    raw_id: bytes = RAW_CREDENTIAL_ID,
    user_handle: bytes = USER_HANDLE,
) -> BrowserCredential:
    encoded = encode_base64url(raw_id)
    return BrowserCredential(
        id=encoded,
        rawId=encoded,
        type="public-key",
        response={"userHandle": encode_base64url(user_handle)},
    )


def build_service(
    *,
    account: Account | None = None,
    identity: ApprovalIdentity | None = None,
    credential: PasskeyCredential | None = None,
    clock: FrozenClock | None = None,
) -> tuple[
    AuthenticationApplicationService,
    MemoryAccountRepository,
    MemoryIdentityRepository,
    MemoryCredentialRepository,
    MemoryAuthStore,
    FakeWebAuthnBackend,
    FrozenClock,
]:
    clock = clock or FrozenClock(NOW)
    accounts = MemoryAccountRepository([account] if account is not None else [])
    identities = MemoryIdentityRepository([identity] if identity is not None else [])
    credentials = MemoryCredentialRepository([credential] if credential is not None else [])
    accounts.identity_records = identities.records
    accounts.credential_records = credentials.records
    store = MemoryAuthStore(clock)
    webauthn = FakeWebAuthnBackend()
    service = AuthenticationApplicationService(
        accounts,  # type: ignore[arg-type]
        identities,  # type: ignore[arg-type]
        credentials,  # type: ignore[arg-type]
        store,
        webauthn,
        challenge_ttl=timedelta(minutes=5),
        session_ttl=timedelta(hours=1),
        clock=clock,
    )
    return service, accounts, identities, credentials, store, webauthn, clock


@pytest.mark.asyncio
async def test_signup_creates_no_db_state_until_verified_then_issues_opaque_session() -> None:
    service, accounts, identities, credentials, store, webauthn, _ = build_service()

    options = await service.signup_options(SignupOptionsCreate(display_name="Test Buyer"))
    state = store.signups[options.challenge_id]
    assert not accounts.records
    assert state.account_id.startswith("acct_")
    assert state.approval_identity_id.startswith("aid_")
    assert len({state.challenge, state.user_handle}) == 2
    assert webauthn.registration_user_handle is not None
    assert encode_base64url(webauthn.registration_user_handle) == state.user_handle

    issued = await service.signup_verify(
        AuthCeremonyVerify(
            challenge_id=options.challenge_id,
            credential=browser_credential(),
        )
    )

    account = next(iter(accounts.records.values()))
    identity = next(iter(identities.records.values()))
    assert account.status is AccountStatus.ACTIVE
    assert account.id == state.account_id
    assert identity.account_id == account.id
    assert next(iter(credentials.records.values())).approval_identity_id == identity.id
    assert issued.session_id.startswith("ses_")
    assert len(issued.session_id) == 47
    assert issued.response.account.id == account.id
    assert issued.response.csrf_token != issued.session_id
    assert "session_id" not in issued.response.model_dump()
    assert store.saved_ttls[-1] == 3600

    with pytest.raises(AuthenticationConflictError) as replay:
        await service.signup_verify(
            AuthCeremonyVerify(
                challenge_id=options.challenge_id,
                credential=browser_credential(),
            )
        )
    assert replay.value.reason_code == "AUTH_SIGNUP_CHALLENGE_ALREADY_USED"


@pytest.mark.asyncio
async def test_signup_uv_failure_and_db_partial_failure_are_fail_closed() -> None:
    service, accounts, _, _, store, webauthn, _ = build_service()
    options = await service.signup_options(SignupOptionsCreate(display_name="Test Buyer"))
    webauthn.registration_verification = RegistrationVerification(
        credential_id=RAW_CREDENTIAL_ID,
        public_key=PUBLIC_KEY,
        sign_count=0,
        transports=(),
        device_type="single_device",
        backed_up=False,
        user_verified=False,
    )
    with pytest.raises(AuthenticationVerificationError) as uv_error:
        await service.signup_verify(
            AuthCeremonyVerify(
                challenge_id=options.challenge_id,
                credential=browser_credential(),
            )
        )
    assert uv_error.value.reason_code == "AUTH_SIGNUP_VERIFICATION_FAILED"
    assert not accounts.records
    assert not store.sessions

    failed, failed_accounts, _, _, failed_store, _, _ = build_service()
    failed_accounts.bundle_error = True
    failed_options = await failed.signup_options(SignupOptionsCreate(display_name="Test Buyer"))
    with pytest.raises(AuthenticationConflictError):
        await failed.signup_verify(
            AuthCeremonyVerify(
                challenge_id=failed_options.challenge_id,
                credential=browser_credential(),
            )
        )
    assert not failed_accounts.records
    assert not failed_store.sessions


@pytest.mark.asyncio
async def test_discoverable_login_maps_credential_server_side_and_commits_before_session() -> None:
    account, identity, credential = make_account(), make_identity(), make_credential()
    service, _, _, credentials, store, webauthn, _ = build_service(
        account=account,
        identity=identity,
        credential=credential,
    )
    options = await service.login_options()
    assert webauthn.authentication_allows is None

    issued = await service.login_verify(
        AuthCeremonyVerify(
            challenge_id=options.challenge_id,
            credential=browser_credential(),
        )
    )

    assert credentials.usage_commits == 1
    assert credential.sign_count == 1
    assert credential.last_used_at == NOW
    assert issued.response.account.id == ACCOUNT_ID
    assert store.sessions[issued.session_id].passkey_credential_id == CREDENTIAL_ID


@pytest.mark.asyncio
async def test_login_unknown_wrong_handle_missing_uv_and_counter_regression_fail_closed() -> None:
    unknown, *_ = build_service()
    unknown_options = await unknown.login_options()
    with pytest.raises(AuthenticationVerificationError) as unknown_error:
        await unknown.login_verify(
            AuthCeremonyVerify(
                challenge_id=unknown_options.challenge_id,
                credential=browser_credential(),
            )
        )
    assert unknown_error.value.reason_code == "AUTH_LOGIN_VERIFICATION_FAILED"

    for failure in ("handle", "uv", "counter", "webauthn"):
        credential = make_credential(sign_count=2 if failure == "counter" else 0)
        service, _, _, credentials, store, webauthn, _ = build_service(
            account=make_account(),
            identity=make_identity(),
            credential=credential,
        )
        options = await service.login_options()
        browser = browser_credential(user_handle=b"wrong" if failure == "handle" else USER_HANDLE)
        if failure == "uv":
            webauthn.authentication_verification = AuthenticationVerification(
                credential_id=RAW_CREDENTIAL_ID,
                new_sign_count=1,
                device_type="single_device",
                backed_up=False,
                user_verified=False,
            )
        elif failure == "counter":
            webauthn.authentication_verification = AuthenticationVerification(
                credential_id=RAW_CREDENTIAL_ID,
                new_sign_count=1,
                device_type="single_device",
                backed_up=False,
                user_verified=True,
            )
        elif failure == "webauthn":
            webauthn.authentication_error = True
        with pytest.raises(AuthenticationVerificationError):
            await service.login_verify(
                AuthCeremonyVerify(challenge_id=options.challenge_id, credential=browser)
            )
        assert credentials.usage_commits == 0
        assert not store.sessions


@pytest.mark.asyncio
async def test_disabled_account_cannot_login_or_use_existing_session() -> None:
    account = make_account(status=AccountStatus.DISABLED)
    service, _, _, _, store, _, _ = build_service(
        account=account,
        identity=make_identity(),
        credential=make_credential(),
    )
    options = await service.login_options()
    with pytest.raises(AuthenticationUnauthorizedError) as login_error:
        await service.login_verify(
            AuthCeremonyVerify(
                challenge_id=options.challenge_id,
                credential=browser_credential(),
            )
        )
    assert login_error.value.reason_code == "AUTH_ACCOUNT_DISABLED"

    state = AuthSessionState(
        session_id=f"ses_{encode_base64url(b's' * 32)}",
        account_id=ACCOUNT_ID,
        approval_identity_id=IDENTITY_ID,
        passkey_credential_id=CREDENTIAL_ID,
        account_session_version=1,
        csrf_token=encode_base64url(b"c" * 32),
        created_at=NOW,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    store.sessions[state.session_id] = state
    with pytest.raises(AuthenticationUnauthorizedError) as session_error:
        await service.resolve_session(state.session_id)
    assert session_error.value.reason_code == "AUTH_ACCOUNT_DISABLED"


@pytest.mark.asyncio
async def test_session_resolution_expiry_version_and_malformed_state_fail_closed() -> None:
    service, accounts, _, _, store, _, clock = build_service(
        account=make_account(),
        identity=make_identity(),
        credential=make_credential(),
    )
    options = await service.login_options()
    issued = await service.login_verify(
        AuthCeremonyVerify(
            challenge_id=options.challenge_id,
            credential=browser_credential(),
        )
    )
    assert (await service.resolve_session(issued.session_id)).account.id == ACCOUNT_ID

    accounts.records[ACCOUNT_ID].session_version += 1
    with pytest.raises(AuthenticationUnauthorizedError) as version_error:
        await service.resolve_session(issued.session_id)
    assert version_error.value.reason_code == "AUTH_SESSION_INVALID"
    accounts.records[ACCOUNT_ID].session_version = 1

    store.invalid_session = True
    with pytest.raises(AuthenticationUnauthorizedError) as malformed:
        await service.resolve_session(issued.session_id)
    assert malformed.value.reason_code == "AUTH_SESSION_INVALID"
    store.invalid_session = False

    clock.current = NOW + timedelta(hours=1)
    with pytest.raises(AuthenticationUnauthorizedError) as expired:
        await service.resolve_session(issued.session_id)
    assert expired.value.reason_code == "AUTH_SESSION_EXPIRED"


@pytest.mark.asyncio
async def test_logout_revokes_server_session_and_passkey_listing_is_safe() -> None:
    service, _, _, _, _, _, _ = build_service(
        account=make_account(),
        identity=make_identity(),
        credential=make_credential(),
    )
    options = await service.login_options()
    issued = await service.login_verify(
        AuthCeremonyVerify(
            challenge_id=options.challenge_id,
            credential=browser_credential(),
        )
    )
    resolved = await service.resolve_session(issued.session_id)
    passkeys = await service.list_passkeys(resolved)
    assert len(passkeys.credentials) == 1
    assert passkeys.credentials[0].current is True
    assert set(passkeys.credentials[0].model_dump()) == {
        "id",
        "transports",
        "created_at",
        "last_used_at",
        "current",
    }

    response = await service.logout(issued.session_id)
    assert response.reason_code == "AUTH_LOGGED_OUT"
    with pytest.raises(AuthenticationUnauthorizedError) as revoked:
        await service.resolve_session(issued.session_id)
    assert revoked.value.reason_code == "AUTH_SESSION_INVALID"


@pytest.mark.asyncio
async def test_challenge_expiry_and_replay_keep_stable_reason_codes() -> None:
    service, _, _, _, _, _, clock = build_service()
    signup = await service.signup_options(SignupOptionsCreate(display_name="Test Buyer"))
    clock.current = NOW + timedelta(minutes=5)
    with pytest.raises(AuthenticationExpiredError) as expired:
        await service.signup_verify(
            AuthCeremonyVerify(
                challenge_id=signup.challenge_id,
                credential=browser_credential(),
            )
        )
    assert expired.value.reason_code == "AUTH_SIGNUP_CHALLENGE_EXPIRED"

    replay_service, *_ = build_service()
    login = await replay_service.login_options()
    payload = AuthCeremonyVerify(
        challenge_id=login.challenge_id,
        credential=browser_credential(),
    )
    with pytest.raises(AuthenticationVerificationError):
        await replay_service.login_verify(payload)
    with pytest.raises(AuthenticationConflictError) as replay:
        await replay_service.login_verify(payload)
    assert replay.value.reason_code == "AUTH_LOGIN_CHALLENGE_ALREADY_USED"


def test_service_rejects_unbounded_ttls() -> None:
    clock = FrozenClock(NOW)
    accounts = MemoryAccountRepository()
    identities = MemoryIdentityRepository()
    credentials = MemoryCredentialRepository()
    store = MemoryAuthStore(clock)
    webauthn = FakeWebAuthnBackend()
    with pytest.raises(ValueError):
        AuthenticationApplicationService(
            accounts,  # type: ignore[arg-type]
            identities,  # type: ignore[arg-type]
            credentials,  # type: ignore[arg-type]
            store,
            webauthn,
            challenge_ttl=timedelta(minutes=11),
            session_ttl=timedelta(hours=1),
            clock=clock,
        )


def test_real_webauthn_discoverable_options_omit_allow_credentials() -> None:
    backend = PyWebAuthnBackend(
        rp_id="localhost",
        rp_name="MeterGate",
        expected_origins=["http://localhost:3000"],
        timeout_ms=300_000,
    )

    options = backend.authentication_options(challenge=b"l" * 32)

    assert options["userVerification"] == "required"
    assert "allowCredentials" not in options
    with pytest.raises(ValueError, match="must not be empty"):
        backend.authentication_options(challenge=b"l" * 32, allow_credentials=[])
