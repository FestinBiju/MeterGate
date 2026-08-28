from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.cache.approval_challenges import (
    ApprovalChallengeState,
    ChallengeAlreadyUsedError,
    ChallengeNotFoundError,
    RegistrationChallengeState,
)
from app.domain.enums import AccountStatus, ApprovalIdentityStatus, PurchaseType
from app.domain.exceptions import (
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalNotFoundError,
    ApprovalVerificationError,
    AuthenticationForbiddenError,
)
from app.domain.hashing import calculate_quote_hash, sha256_bytes, sha256_json
from app.domain.policy_engine import POLICY_EVALUATION_VERSION, evaluate_policy
from app.domain.policy_hashing import POLICY_VERSION, calculate_policy_hash
from app.models import (
    Account,
    ApprovalIdentity,
    BuyerPolicy,
    PasskeyCredential,
    PolicyEvaluation,
    PurchaseAuthorization,
    Quote,
)
from app.schemas.approvals import (
    ApprovalAssertionVerify,
    ApprovalChallengeCreate,
    BrowserCredential,
    PasskeyRegistrationVerify,
)
from app.services.approvals import ApprovalApplicationService
from app.services.passkeys import PasskeyApplicationService
from app.services.webauthn import (
    AuthenticationVerification,
    RegistrationVerification,
    WebAuthnCredentialDescriptor,
    WebAuthnVerificationError,
)

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
ACCOUNT_ID = "acct_00000000000000000000000001"
OTHER_ACCOUNT_ID = "acct_00000000000000000000000002"
IDENTITY_ID = "aid_00000000000000000000000001"
OTHER_IDENTITY_ID = "aid_00000000000000000000000002"
CREDENTIAL_ID = "pkc_00000000000000000000000001"
POLICY_ID = "pol_00000000000000000000000001"
QUOTE_ID = "qte_00000000000000000000000001"
EVALUATION_ID = "pye_00000000000000000000000001"
MERCHANT_ID = "mrc_00000000000000000000000001"
SERVICE_ID = "svc_00000000000000000000000001"
RAW_CREDENTIAL_ID = b"credential-id-1"
PUBLIC_KEY = b"cose-public-key"
SESSION_ID = "ses_00000000000000000000000001"
SESSION_VERSION = 1


@dataclass
class FrozenClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


class MemoryIdentityRepository:
    def __init__(self, identities: Sequence[ApprovalIdentity] = ()) -> None:
        self.records = {identity.id: identity for identity in identities}

    async def create(self, identity: ApprovalIdentity) -> ApprovalIdentity:
        identity.id = IDENTITY_ID
        identity.webauthn_user_handle = b"u" * 32
        identity.created_at = NOW
        identity.updated_at = NOW
        self.records[identity.id] = identity
        return identity

    async def get(self, identity_id: str) -> ApprovalIdentity | None:
        return self.records.get(identity_id)

    async def get_for_update(self, identity_id: str) -> ApprovalIdentity | None:
        return self.records.get(identity_id)

    async def get_by_account_id_for_update(
        self,
        account_id: str,
    ) -> ApprovalIdentity | None:
        return next(
            (identity for identity in self.records.values() if identity.account_id == account_id),
            None,
        )


class MemoryAccountRepository:
    def __init__(self, accounts: Sequence[Account] = ()) -> None:
        self.records = {account.id: account for account in accounts}

    async def get(self, account_id: str) -> Account | None:
        return self.records.get(account_id)

    async def get_for_update(self, account_id: str) -> Account | None:
        return self.records.get(account_id)


class MemoryCredentialRepository:
    def __init__(self, credentials: Sequence[PasskeyCredential] = ()) -> None:
        self.records = {credential.id: credential for credential in credentials}

    async def create(self, credential: PasskeyCredential) -> PasskeyCredential:
        credential.id = CREDENTIAL_ID
        credential.created_at = NOW
        credential.last_used_at = None
        self.records[credential.id] = credential
        return credential

    async def list_for_identity(self, identity_id: str) -> list[PasskeyCredential]:
        return [
            credential
            for credential in self.records.values()
            if credential.approval_identity_id == identity_id
        ]

    async def get_by_credential_id_for_update(
        self,
        credential_id: bytes,
        *,
        identity_id: str,
    ) -> PasskeyCredential | None:
        return next(
            (
                credential
                for credential in self.records.values()
                if credential.credential_id == credential_id
                and credential.approval_identity_id == identity_id
            ),
            None,
        )


class MemoryChallengeStore:
    def __init__(self) -> None:
        self.registrations: dict[str, RegistrationChallengeState] = {}
        self.approvals: dict[str, ApprovalChallengeState] = {}
        self.used_registrations: set[tuple[str, str]] = set()
        self.used_approvals: set[tuple[str, str]] = set()

    async def save_registration(
        self,
        state: RegistrationChallengeState,
        *,
        ttl_seconds: int,
    ) -> None:
        assert ttl_seconds > 0
        self.registrations[state.challenge_id] = state

    async def consume_registration(
        self,
        challenge_id: str,
        *,
        account_id: str,
    ) -> RegistrationChallengeState:
        key = (account_id, challenge_id)
        if key in self.used_registrations:
            raise ChallengeAlreadyUsedError(challenge_id)
        state = self.registrations.get(challenge_id)
        if state is None or state.account_id != account_id:
            raise ChallengeNotFoundError(challenge_id)
        self.registrations.pop(challenge_id)
        self.used_registrations.add(key)
        return state

    async def save_approval(
        self,
        state: ApprovalChallengeState,
        *,
        ttl_seconds: int,
    ) -> None:
        assert ttl_seconds > 0
        self.approvals[state.challenge_id] = state

    async def consume_approval(
        self,
        challenge_id: str,
        *,
        account_id: str,
    ) -> ApprovalChallengeState:
        key = (account_id, challenge_id)
        if key in self.used_approvals:
            raise ChallengeAlreadyUsedError(challenge_id)
        state = self.approvals.get(challenge_id)
        if state is None or state.account_id != account_id:
            raise ChallengeNotFoundError(challenge_id)
        self.approvals.pop(challenge_id)
        self.used_approvals.add(key)
        return state


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
        self.authentication_options_hook: Callable[[], None] | None = None
        self.authentication_hook: Callable[[], None] | None = None
        self.authentication_verifications = 0
        self.last_registration_challenge: bytes | None = None
        self.last_authentication_challenge: bytes | None = None
        self.registration_excludes: Sequence[WebAuthnCredentialDescriptor] = ()
        self.authentication_allows: Sequence[WebAuthnCredentialDescriptor] = ()

    def registration_options(
        self,
        *,
        challenge: bytes,
        user_handle: bytes,
        user_name: str,
        user_display_name: str,
        exclude_credentials: Sequence[WebAuthnCredentialDescriptor] = (),
    ) -> dict[str, Any]:
        assert user_handle == b"u" * 32
        assert user_name == "dev-user-001"
        assert user_display_name == "Development User"
        self.last_registration_challenge = challenge
        self.registration_excludes = exclude_credentials
        return {"challenge": "server-generated", "userVerification": "required"}

    def verify_registration(
        self,
        *,
        credential: Mapping[str, Any],
        expected_challenge: bytes,
    ) -> RegistrationVerification:
        assert credential["type"] == "public-key"
        self.last_registration_challenge = expected_challenge
        if self.registration_error:
            raise WebAuthnVerificationError()
        return self.registration_verification

    def authentication_options(
        self,
        *,
        challenge: bytes,
        allow_credentials: Sequence[WebAuthnCredentialDescriptor],
    ) -> dict[str, Any]:
        self.last_authentication_challenge = challenge
        self.authentication_allows = allow_credentials
        if self.authentication_options_hook is not None:
            self.authentication_options_hook()
        return {"challenge": "server-generated", "userVerification": "required"}

    def verify_authentication(
        self,
        *,
        credential: Mapping[str, Any],
        expected_challenge: bytes,
        credential_public_key: bytes,
        credential_current_sign_count: int,
    ) -> AuthenticationVerification:
        self.authentication_verifications += 1
        assert credential["type"] == "public-key"
        assert credential_public_key == PUBLIC_KEY
        assert credential_current_sign_count == 0
        self.last_authentication_challenge = expected_challenge
        if self.authentication_hook is not None:
            self.authentication_hook()
        if self.authentication_error:
            raise WebAuthnVerificationError()
        return self.authentication_verification


class ObjectRepository:
    def __init__(self, records: Sequence[Any]) -> None:
        self.records = {record.id: record for record in records}

    async def get(self, record_id: str) -> Any | None:
        return self.records.get(record_id)


class MemoryAuthorizationRepository(ObjectRepository):
    def __init__(self) -> None:
        super().__init__(())

    async def create_with_locked_credential_update(
        self,
        authorization: PurchaseAuthorization,
        *,
        credential: PasskeyCredential,
        new_sign_count: int,
        last_used_at: datetime,
    ) -> PurchaseAuthorization:
        assert authorization.passkey_credential_id == credential.id
        credential.sign_count = new_sign_count
        credential.last_used_at = last_used_at
        authorization.created_at = last_used_at
        self.records[authorization.id] = authorization
        return authorization


def make_identity(
    *,
    identity_id: str = IDENTITY_ID,
    account_id: str = ACCOUNT_ID,
    subject_ref: str = "dev-user-001",
    status: ApprovalIdentityStatus = ApprovalIdentityStatus.ACTIVE,
) -> ApprovalIdentity:
    return ApprovalIdentity(
        id=identity_id,
        account_id=account_id,
        subject_ref=subject_ref,
        display_name="Development User",
        webauthn_user_handle=b"u" * 32,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


def make_account(
    *,
    account_id: str = ACCOUNT_ID,
    status: AccountStatus = AccountStatus.ACTIVE,
    session_version: int = SESSION_VERSION,
) -> Account:
    return Account(
        id=account_id,
        display_name="Development User",
        status=status,
        session_version=session_version,
        created_at=NOW,
        updated_at=NOW,
    )


def make_credential(
    *,
    raw_id: bytes = RAW_CREDENTIAL_ID,
    identity_id: str = IDENTITY_ID,
) -> PasskeyCredential:
    return PasskeyCredential(
        id=CREDENTIAL_ID,
        approval_identity_id=identity_id,
        credential_id=raw_id,
        public_key=PUBLIC_KEY,
        sign_count=0,
        transports=["internal"],
        created_at=NOW,
        last_used_at=None,
    )


def make_policy(
    *,
    subject_ref: str = "dev-user-001",
    maximum_amount: int = 1000,
    expires_at: datetime = NOW + timedelta(minutes=15),
) -> BuyerPolicy:
    fields: dict[str, Any] = {
        "policy_id": POLICY_ID,
        "subject_ref": subject_ref,
        "maximum_amount": maximum_amount,
        "allowed_currencies": ["INR"],
        "allowed_merchant_ids": [MERCHANT_ID],
        "allowed_service_ids": [SERVICE_ID],
        "allowed_service_types": ["report"],
        "allowed_purchase_types": ["one_time"],
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": expires_at,
        "policy_version": POLICY_VERSION,
    }
    return BuyerPolicy(
        id=POLICY_ID,
        subject_ref=subject_ref,
        maximum_amount=maximum_amount,
        allowed_currencies=fields["allowed_currencies"],
        allowed_merchant_ids=fields["allowed_merchant_ids"],
        allowed_service_ids=fields["allowed_service_ids"],
        allowed_service_types=fields["allowed_service_types"],
        allowed_purchase_types=fields["allowed_purchase_types"],
        issued_at=fields["issued_at"],
        expires_at=expires_at,
        policy_version=POLICY_VERSION,
        policy_hash=calculate_policy_hash(**fields),
        created_at=NOW,
    )


def make_quote(*, expires_at: datetime = NOW + timedelta(minutes=5)) -> Quote:
    snapshot = {
        "version": "1",
        "json_schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        "merchant": {"id": MERCHANT_ID, "slug": "orbitintel", "name": "OrbitIntel"},
        "service": {
            "id": SERVICE_ID,
            "slug": "orbital-risk-report",
            "name": "Orbital Risk Report",
            "service_type": "report",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "output_content_type": "application/json",
        },
        "pricing": {"amount": 500, "currency": "INR", "purchase_type": "one_time"},
        "fulfillment": {"maximum_seconds": 30, "refund_on_failure": True},
    }
    input_value = {"norad_id": 25544}
    input_hash = sha256_json(input_value)
    fields = {
        "quote_id": QUOTE_ID,
        "merchant_id": MERCHANT_ID,
        "service_id": SERVICE_ID,
        "service_snapshot": snapshot,
        "input_value": input_value,
        "input_hash": input_hash,
        "amount": 500,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "maximum_fulfillment_seconds": 30,
        "refund_on_fulfillment_failure": True,
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": expires_at,
    }
    return Quote(
        id=QUOTE_ID,
        merchant_id=MERCHANT_ID,
        service_id=SERVICE_ID,
        service_snapshot=snapshot,
        input=input_value,
        input_hash=input_hash,
        amount=500,
        currency="INR",
        purchase_type=PurchaseType.ONE_TIME,
        maximum_fulfillment_seconds=30,
        refund_on_fulfillment_failure=True,
        issued_at=fields["issued_at"],
        expires_at=expires_at,
        created_at=NOW,
        quote_hash=calculate_quote_hash(**fields),
    )


def make_evaluation(policy: BuyerPolicy, quote: Quote) -> PolicyEvaluation:
    result = evaluate_policy(policy, quote, NOW)
    return PolicyEvaluation(
        id=EVALUATION_ID,
        policy_id=POLICY_ID,
        quote_id=QUOTE_ID,
        policy_hash=policy.policy_hash,
        quote_hash=quote.quote_hash,
        decision=result.decision,
        checks=[check.as_dict() for check in result.checks],
        evaluated_at=NOW,
        evaluation_version=POLICY_EVALUATION_VERSION,
        created_at=NOW,
    )


def browser_credential(raw_id: bytes = RAW_CREDENTIAL_ID) -> BrowserCredential:
    from app.domain.base64url import encode_base64url

    encoded = encode_base64url(raw_id)
    return BrowserCredential(
        id=encoded,
        rawId=encoded,
        type="public-key",
        response={},
    )


def passkey_service(
    *,
    identity: ApprovalIdentity | None = None,
    credentials: Sequence[PasskeyCredential] = (),
) -> tuple[
    PasskeyApplicationService,
    MemoryIdentityRepository,
    MemoryCredentialRepository,
    MemoryChallengeStore,
    FakeWebAuthnBackend,
]:
    identities = [identity or make_identity()]
    identity_repo = MemoryIdentityRepository(identities)
    credential_repo = MemoryCredentialRepository(credentials)
    store = MemoryChallengeStore()
    webauthn = FakeWebAuthnBackend()
    service = PasskeyApplicationService(
        MemoryAccountRepository([make_account(account_id=identities[0].account_id)]),  # type: ignore[arg-type]
        identity_repo,  # type: ignore[arg-type]
        credential_repo,  # type: ignore[arg-type]
        store,
        webauthn,
        challenge_ttl=timedelta(minutes=5),
        clock=FrozenClock(NOW),
    )
    return service, identity_repo, credential_repo, store, webauthn


def approval_service(
    *,
    policy: BuyerPolicy | None = None,
    quote: Quote | None = None,
    identity: ApprovalIdentity | None = None,
    credentials: Sequence[PasskeyCredential] | None = None,
    clock: FrozenClock | None = None,
) -> tuple[
    ApprovalApplicationService,
    MemoryChallengeStore,
    FakeWebAuthnBackend,
    MemoryAuthorizationRepository,
    FrozenClock,
]:
    policy = policy or make_policy()
    quote = quote or make_quote()
    evaluation = make_evaluation(policy, quote)
    identity = identity or make_identity()
    credentials = list(credentials) if credentials is not None else [make_credential()]
    store = MemoryChallengeStore()
    webauthn = FakeWebAuthnBackend()
    authorizations = MemoryAuthorizationRepository()
    clock = clock or FrozenClock(NOW)
    service = ApprovalApplicationService(
        MemoryAccountRepository([make_account(account_id=identity.account_id)]),  # type: ignore[arg-type]
        MemoryIdentityRepository([identity]),  # type: ignore[arg-type]
        MemoryCredentialRepository(credentials),  # type: ignore[arg-type]
        authorizations,  # type: ignore[arg-type]
        ObjectRepository([policy]),  # type: ignore[arg-type]
        ObjectRepository([quote]),  # type: ignore[arg-type]
        ObjectRepository([evaluation]),  # type: ignore[arg-type]
        store,
        webauthn,
        challenge_ttl=timedelta(minutes=5),
        authorization_ttl=timedelta(seconds=120),
        clock=clock,
    )
    return service, store, webauthn, authorizations, clock


async def registration_options(
    service: PasskeyApplicationService,
    identity_id: str = IDENTITY_ID,
) -> Any:
    return await service.registration_options(
        identity_id,
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
    )


async def verify_registration(
    service: PasskeyApplicationService,
    identity_id: str,
    payload: PasskeyRegistrationVerify,
) -> Any:
    return await service.verify_registration(
        identity_id,
        payload,
        account_id=ACCOUNT_ID,
        account_session_version=SESSION_VERSION,
        session_id=SESSION_ID,
    )


async def create_approval_challenge(service: ApprovalApplicationService) -> Any:
    return await service.create_challenge(
        ApprovalChallengeCreate(evaluation_id=EVALUATION_ID),
        account_id=ACCOUNT_ID,
        account_session_version=SESSION_VERSION,
    )


async def verify_approval_challenge(
    service: ApprovalApplicationService,
    challenge_id: str,
    payload: ApprovalAssertionVerify,
) -> Any:
    return await service.verify_challenge(
        challenge_id,
        payload,
        account_id=ACCOUNT_ID,
        account_session_version=SESSION_VERSION,
    )


@pytest.mark.asyncio
async def test_identity_get_returns_only_safe_account_bound_metadata() -> None:
    service, _, _, _, _ = passkey_service()

    fetched = await service.get_identity(
        IDENTITY_ID,
        account_id=ACCOUNT_ID,
    )

    assert fetched.status is ApprovalIdentityStatus.ACTIVE
    assert fetched.account_id == ACCOUNT_ID
    assert fetched.credential_count == 0
    assert set(fetched.model_dump()) == {
        "id",
        "account_id",
        "subject_ref",
        "display_name",
        "status",
        "credential_count",
        "created_at",
        "updated_at",
    }


@pytest.mark.asyncio
async def test_account_cannot_get_or_enroll_another_accounts_approval_identity() -> None:
    service, _, _, _, _ = passkey_service()

    with pytest.raises(AuthenticationForbiddenError) as get_error:
        await service.get_identity(IDENTITY_ID, account_id=OTHER_ACCOUNT_ID)
    with pytest.raises(AuthenticationForbiddenError) as enroll_error:
        await service.registration_options(
            IDENTITY_ID,
            account_id=OTHER_ACCOUNT_ID,
            session_id=SESSION_ID,
        )

    assert get_error.value.reason_code == "AUTH_RESOURCE_OWNERSHIP_MISMATCH"
    assert enroll_error.value.reason_code == "AUTH_RESOURCE_OWNERSHIP_MISMATCH"


@pytest.mark.asyncio
async def test_registration_binds_challenge_identity_and_excludes_existing_credentials() -> None:
    existing = make_credential()
    service, _, _, store, webauthn = passkey_service(credentials=[existing])

    options = await registration_options(service)

    state = store.registrations[options.challenge_id]
    assert state.account_id == ACCOUNT_ID
    assert state.approval_identity_id == IDENTITY_ID
    assert state.session_id_hash == sha256_bytes(SESSION_ID.encode())
    assert state.user_handle
    assert webauthn.registration_excludes == (
        WebAuthnCredentialDescriptor(RAW_CREDENTIAL_ID, ("internal",)),
    )
    assert options.public_key["userVerification"] == "required"


@pytest.mark.asyncio
async def test_registration_verification_persists_safe_credential_and_consumes_challenge() -> None:
    service, _, credentials, _, _ = passkey_service()
    options = await registration_options(service)
    payload = PasskeyRegistrationVerify(
        challenge_id=options.challenge_id,
        credential=browser_credential(),
    )

    response = await verify_registration(service, IDENTITY_ID, payload)

    assert response.status == "registered"
    assert response.credential.id == CREDENTIAL_ID
    assert response.credential.transports == ["internal"]
    assert set(response.credential.model_dump()) == {
        "id",
        "transports",
        "created_at",
        "last_used_at",
    }
    assert credentials.records[CREDENTIAL_ID].public_key == PUBLIC_KEY
    with pytest.raises(ApprovalConflictError, match="already used"):
        await verify_registration(service, IDENTITY_ID, payload)


@pytest.mark.asyncio
async def test_registration_rejects_disabled_identity_and_missing_uv() -> None:
    disabled, *_ = passkey_service(identity=make_identity(status=ApprovalIdentityStatus.DISABLED))
    with pytest.raises(ApprovalConflictError) as disabled_error:
        await registration_options(disabled)
    assert disabled_error.value.reason_code == "APPROVAL_IDENTITY_DISABLED"

    service, _, _, _, webauthn = passkey_service()
    options = await registration_options(service)
    webauthn.registration_verification = RegistrationVerification(
        credential_id=RAW_CREDENTIAL_ID,
        public_key=PUBLIC_KEY,
        sign_count=0,
        transports=(),
        device_type="single_device",
        backed_up=False,
        user_verified=False,
    )
    with pytest.raises(ApprovalVerificationError) as uv_error:
        await verify_registration(
            service,
            IDENTITY_ID,
            PasskeyRegistrationVerify(
                challenge_id=options.challenge_id,
                credential=browser_credential(),
            ),
        )
    assert uv_error.value.reason_code == "APPROVAL_USER_VERIFICATION_REQUIRED"


@pytest.mark.asyncio
async def test_registration_challenge_cannot_move_between_identities() -> None:
    service, _, _, _, _ = passkey_service()
    options = await registration_options(service)
    payload = PasskeyRegistrationVerify(
        challenge_id=options.challenge_id,
        credential=browser_credential(),
    )

    with pytest.raises(ApprovalConflictError) as error:
        await verify_registration(service, OTHER_IDENTITY_ID, payload)

    assert error.value.reason_code == "PASSKEY_IDENTITY_BINDING_MISMATCH"
    with pytest.raises(ApprovalConflictError) as replay:
        await verify_registration(service, IDENTITY_ID, payload)
    assert replay.value.reason_code == "PASSKEY_CHALLENGE_ALREADY_USED"


@pytest.mark.asyncio
async def test_other_account_cannot_burn_a_passkey_registration_challenge() -> None:
    service, _, _, _, _ = passkey_service()
    options = await registration_options(service)
    payload = PasskeyRegistrationVerify(
        challenge_id=options.challenge_id,
        credential=browser_credential(),
    )

    with pytest.raises(ApprovalNotFoundError) as other_account:
        await service.verify_registration(
            IDENTITY_ID,
            payload,
            account_id=OTHER_ACCOUNT_ID,
            account_session_version=SESSION_VERSION,
            session_id=SESSION_ID,
        )

    registered = await verify_registration(service, IDENTITY_ID, payload)
    assert other_account.value.reason_code == "PASSKEY_CHALLENGE_NOT_FOUND"
    assert registered.credential.id == CREDENTIAL_ID


@pytest.mark.asyncio
async def test_approval_challenge_requires_allow_matching_active_subject_and_passkey() -> None:
    denied_policy = make_policy(maximum_amount=100)
    denied, *_ = approval_service(policy=denied_policy)
    with pytest.raises(ApprovalConflictError) as denied_error:
        await create_approval_challenge(denied)
    assert denied_error.value.reason_code == "APPROVAL_EVALUATION_NOT_ALLOWED"

    mismatched, *_ = approval_service(identity=make_identity(subject_ref="someone-else"))
    with pytest.raises(AuthenticationForbiddenError) as subject_error:
        await create_approval_challenge(mismatched)
    assert subject_error.value.reason_code == "AUTH_RESOURCE_OWNERSHIP_MISMATCH"

    disabled, *_ = approval_service(identity=make_identity(status=ApprovalIdentityStatus.DISABLED))
    with pytest.raises(ApprovalConflictError) as disabled_error:
        await create_approval_challenge(disabled)
    assert disabled_error.value.reason_code == "APPROVAL_IDENTITY_DISABLED"

    no_passkey, *_ = approval_service(credentials=[])
    with pytest.raises(ApprovalConflictError) as passkey_error:
        await create_approval_challenge(no_passkey)
    assert passkey_error.value.reason_code == "APPROVAL_PASSKEY_REQUIRED"


@pytest.mark.asyncio
async def test_account_cannot_create_a_challenge_for_another_accounts_evaluation() -> None:
    account_b_identity = make_identity(
        account_id=OTHER_ACCOUNT_ID,
        subject_ref=OTHER_ACCOUNT_ID,
    )
    service, *_ = approval_service(
        policy=make_policy(subject_ref=ACCOUNT_ID, maximum_amount=100),
        identity=account_b_identity,
    )

    with pytest.raises(AuthenticationForbiddenError) as error:
        await service.create_challenge(
            ApprovalChallengeCreate(evaluation_id=EVALUATION_ID),
            account_id=OTHER_ACCOUNT_ID,
            account_session_version=SESSION_VERSION,
        )

    assert error.value.reason_code == "AUTH_RESOURCE_OWNERSHIP_MISMATCH"


@pytest.mark.asyncio
async def test_approval_challenge_returns_server_review_and_credential_allowlist() -> None:
    service, store, webauthn, _, _ = approval_service()

    response = await create_approval_challenge(service)

    assert response.review.subject_ref == "dev-user-001"
    assert response.review.merchant.name == "OrbitIntel"
    assert response.review.service.name == "Orbital Risk Report"
    assert response.review.amount == 500
    assert len(response.review.policy_checks) == 10
    assert response.review_hash == store.approvals[response.challenge_id].review_hash
    assert webauthn.authentication_allows == (
        WebAuthnCredentialDescriptor(RAW_CREDENTIAL_ID, ("internal",)),
    )
    assert "credential_id" not in str(response.model_dump()).lower()


@pytest.mark.asyncio
async def test_verified_approval_creates_hashed_short_lived_authorization_and_updates_counter() -> (
    None
):
    service, _, _, authorizations, _ = approval_service()
    challenge = await create_approval_challenge(service)

    response = await verify_approval_challenge(
        service,
        challenge.challenge_id,
        ApprovalAssertionVerify(credential=browser_credential()),
    )

    persisted = authorizations.records[response.id]
    assert response.state == "active"
    assert response.expires_at == NOW + timedelta(seconds=120)
    assert response.authorization_hash == persisted.authorization_hash
    assert response.review_hash == challenge.review_hash
    assert persisted.challenge_hash.startswith("sha256:")
    assert persisted.passkey_credential_id == CREDENTIAL_ID
    assert set(response.model_dump()).isdisjoint(
        {"passkey_credential_id", "challenge_hash", "signature", "public_key"}
    )


@pytest.mark.asyncio
async def test_approval_rejects_wrong_credential_uv_failure_and_bad_assertion() -> None:
    service, _, webauthn, _, _ = approval_service()
    wrong = await create_approval_challenge(service)
    with pytest.raises(ApprovalConflictError) as wrong_error:
        await verify_approval_challenge(
            service,
            wrong.challenge_id,
            ApprovalAssertionVerify(credential=browser_credential(b"other")),
        )
    assert wrong_error.value.reason_code == "APPROVAL_CREDENTIAL_NOT_FOUND"

    uv = await create_approval_challenge(service)
    webauthn.authentication_verification = AuthenticationVerification(
        credential_id=RAW_CREDENTIAL_ID,
        new_sign_count=1,
        device_type="single_device",
        backed_up=False,
        user_verified=False,
    )
    with pytest.raises(ApprovalVerificationError) as uv_error:
        await verify_approval_challenge(
            service,
            uv.challenge_id,
            ApprovalAssertionVerify(credential=browser_credential()),
        )
    assert uv_error.value.reason_code == "APPROVAL_USER_VERIFICATION_REQUIRED"

    malformed = await create_approval_challenge(service)
    webauthn.authentication_error = True
    with pytest.raises(ApprovalVerificationError) as verification_error:
        await verify_approval_challenge(
            service,
            malformed.challenge_id,
            ApprovalAssertionVerify(credential=browser_credential()),
        )
    assert verification_error.value.reason_code == "APPROVAL_WEBAUTHN_VERIFICATION_FAILED"


@pytest.mark.asyncio
async def test_approval_review_binding_tamper_fails_before_webauthn() -> None:
    service, store, webauthn, _, _ = approval_service()
    challenge = await create_approval_challenge(service)
    state = store.approvals[challenge.challenge_id]
    store.approvals[challenge.challenge_id] = state.model_copy(update={"amount": state.amount + 1})

    with pytest.raises(ApprovalConflictError) as error:
        await verify_approval_challenge(
            service,
            challenge.challenge_id,
            ApprovalAssertionVerify(credential=browser_credential()),
        )

    assert error.value.reason_code == "APPROVAL_REVIEW_HASH_MISMATCH"
    assert webauthn.authentication_verifications == 0


@pytest.mark.asyncio
async def test_approval_revalidates_freshness_after_webauthn_to_close_toctou() -> None:
    clock = FrozenClock(NOW)
    policy = make_policy(expires_at=NOW + timedelta(seconds=1))
    quote = make_quote(expires_at=NOW + timedelta(seconds=2))
    service, _, webauthn, authorizations, _ = approval_service(
        policy=policy,
        quote=quote,
        clock=clock,
    )
    challenge = await create_approval_challenge(service)
    webauthn.authentication_hook = lambda: setattr(
        clock,
        "current",
        NOW + timedelta(seconds=1),
    )

    with pytest.raises(ApprovalExpiredError) as error:
        await verify_approval_challenge(
            service,
            challenge.challenge_id,
            ApprovalAssertionVerify(credential=browser_credential()),
        )

    assert error.value.reason_code == "APPROVAL_POLICY_EXPIRED"
    assert not authorizations.records


@pytest.mark.asyncio
async def test_approval_rejects_challenge_that_expires_during_final_replay() -> None:
    clock = FrozenClock(NOW)
    credential = make_credential()
    service, _, webauthn, authorizations, _ = approval_service(
        quote=make_quote(expires_at=NOW + timedelta(minutes=10)),
        credentials=[credential],
        clock=clock,
    )
    challenge = await create_approval_challenge(service)
    webauthn.authentication_hook = lambda: setattr(
        clock,
        "current",
        NOW + timedelta(minutes=5),
    )

    with pytest.raises(ApprovalExpiredError) as error:
        await verify_approval_challenge(
            service,
            challenge.challenge_id,
            ApprovalAssertionVerify(credential=browser_credential()),
        )

    assert error.value.reason_code == "APPROVAL_CHALLENGE_EXPIRED"
    assert not authorizations.records
    assert credential.sign_count == 0
    assert credential.last_used_at is None


@pytest.mark.asyncio
async def test_approval_issuance_refuses_context_expiring_during_option_generation() -> None:
    clock = FrozenClock(NOW)
    service, store, webauthn, authorizations, _ = approval_service(
        policy=make_policy(expires_at=NOW + timedelta(seconds=1)),
        quote=make_quote(expires_at=NOW + timedelta(seconds=2)),
        clock=clock,
    )
    webauthn.authentication_options_hook = lambda: setattr(
        clock,
        "current",
        NOW + timedelta(seconds=1),
    )

    with pytest.raises(ApprovalExpiredError) as error:
        await create_approval_challenge(service)

    assert error.value.reason_code == "APPROVAL_POLICY_EXPIRED"
    assert not store.approvals
    assert not authorizations.records


@pytest.mark.asyncio
async def test_approval_challenge_replay_cannot_create_second_authorization() -> None:
    service, _, _, authorizations, _ = approval_service()
    challenge = await create_approval_challenge(service)
    payload = ApprovalAssertionVerify(credential=browser_credential())
    await verify_approval_challenge(service, challenge.challenge_id, payload)

    with pytest.raises(ApprovalConflictError) as error:
        await verify_approval_challenge(service, challenge.challenge_id, payload)

    assert error.value.reason_code == "APPROVAL_CHALLENGE_ALREADY_USED"
    assert len(authorizations.records) == 1


@pytest.mark.asyncio
async def test_other_account_cannot_burn_challenge_or_get_authorization() -> None:
    service, _, _, _, _ = approval_service()
    challenge = await create_approval_challenge(service)
    assertion = ApprovalAssertionVerify(credential=browser_credential())

    with pytest.raises(ApprovalNotFoundError) as challenge_error:
        await service.verify_challenge(
            challenge.challenge_id,
            assertion,
            account_id=OTHER_ACCOUNT_ID,
            account_session_version=SESSION_VERSION,
        )

    created = await verify_approval_challenge(service, challenge.challenge_id, assertion)
    with pytest.raises(AuthenticationForbiddenError) as authorization_error:
        await service.get_authorization(created.id, account_id=OTHER_ACCOUNT_ID)

    assert challenge_error.value.reason_code == "APPROVAL_CHALLENGE_NOT_FOUND"
    assert authorization_error.value.reason_code == "AUTH_RESOURCE_OWNERSHIP_MISMATCH"


@pytest.mark.asyncio
async def test_get_authorization_derives_expiry_and_recomputes_integrity() -> None:
    service, _, _, authorizations, clock = approval_service()
    challenge = await create_approval_challenge(service)
    created = await verify_approval_challenge(
        service,
        challenge.challenge_id,
        ApprovalAssertionVerify(credential=browser_credential()),
    )
    clock.current = NOW + timedelta(seconds=121)

    fetched = await service.get_authorization(created.id, account_id=ACCOUNT_ID)
    assert fetched.state == "expired"

    authorizations.records[created.id].authorization_hash = f"sha256:{'f' * 64}"
    with pytest.raises(ApprovalIntegrityError) as error:
        await service.get_authorization(created.id, account_id=ACCOUNT_ID)
    assert error.value.reason_code == "INTEGRITY_AUTHORIZATION_HASH_MISMATCH"
