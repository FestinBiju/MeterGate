from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.cache.approval_challenges import ChallengeAlreadyUsedError
from app.cache.human_presence import HumanPresenceChallengeState, RedisHumanPresenceStore
from app.core.config import Settings
from app.domain.base64url import encode_base64url
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
    calculate_presence_hash,
    normalize_resource_binding,
    requires_human_presence,
)
from app.models import Account, HumanPresenceProof, PasskeyCredential
from app.schemas.human_presence import HumanPresenceVerify
from app.services.human_presence import HumanPresenceService
from app.services.webauthn import AuthenticationVerification

ACCOUNT_ID = "acct_00000000000000000000000001"
PROOF_ID = "hpp_00000000000000000000000001"
CREDENTIAL_ID = "pkc_00000000000000000000000001"
SESSION_ID = "ses_" + "a" * 43
ORIGIN = "https://app.example.test"
BINDING = {"scopes": ["mcp:catalog.read"], "expires_in_seconds": 900}
NOW = datetime.now(UTC)


class ScalarSession:
    def __init__(self, proof: HumanPresenceProof | None) -> None:
        self.proof = proof

    async def scalar(self, _statement: object):
        return self.proof


class SequenceSession:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.added: list[object] = []
        self.commits = 0

    async def scalar(self, _statement: object):
        return self.values.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1


class MemoryStore:
    def __init__(self, state: HumanPresenceChallengeState) -> None:
        self.state = state
        self.used = False

    async def consume(self, _challenge_id: str, *, account_id: str):
        if self.used:
            raise ChallengeAlreadyUsedError(self.state.challenge_id)
        assert account_id == self.state.account_id
        self.used = True
        return self.state


class FakeWebAuthn:
    def __init__(self, raw_id: bytes, *, user_verified: bool = True) -> None:
        self.raw_id = raw_id
        self.user_verified = user_verified

    def verify_authentication(self, **_: object) -> AuthenticationVerification:
        return AuthenticationVerification(
            credential_id=self.raw_id,
            new_sign_count=2,
            device_type="single_device",
            backed_up=False,
            user_verified=self.user_verified,
        )


class ScriptRedis:
    def __init__(self) -> None:
        self.state: bytes | None = None
        self.status: str | None = None

    async def eval(self, script: str, _numkeys: int, *args: object):
        if "ARGV[2]" in script:
            if self.state is not None or self.status is not None:
                return 0
            self.state = args[2]  # type: ignore[assignment]
            self.status = "issued"
            return 1
        if self.state is not None:
            payload, self.state = self.state, None
            self.status = "consumed"
            return [1, payload]
        if self.status == "consumed":
            return [2, False]
        return [0, False]


def proof(*, expires_at: datetime = NOW + timedelta(seconds=120)) -> HumanPresenceProof:
    issued_at = NOW
    session_hash = sha256_bytes(SESSION_ID.encode())
    challenge_hash = sha256_bytes(b"challenge" * 4)
    return HumanPresenceProof(
        id=PROOF_ID,
        account_id=ACCOUNT_ID,
        session_id_hash=session_hash,
        passkey_credential_id=CREDENTIAL_ID,
        action_class="new_agent_session",
        resource_binding=BINDING,
        origin=ORIGIN,
        challenge_hash=challenge_hash,
        presence_version=HUMAN_PRESENCE_VERSION,
        presence_hash=calculate_presence_hash(
            proof_id=PROOF_ID,
            account_id=ACCOUNT_ID,
            session_id_hash=session_hash,
            passkey_credential_id=CREDENTIAL_ID,
            action_class="new_agent_session",
            resource_binding=BINDING,
            origin=ORIGIN,
            issued_at=issued_at,
            expires_at=expires_at,
            challenge_hash=challenge_hash,
        ),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def challenge_state(raw_id: bytes) -> HumanPresenceChallengeState:
    return HumanPresenceChallengeState(
        challenge_id="hpc_00000000000000000000000001",
        challenge=encode_base64url(b"c" * 32),
        account_id=ACCOUNT_ID,
        session_id_hash=sha256_bytes(SESSION_ID.encode()),
        action_class="new_agent_session",
        resource_binding=BINDING,
        resource_binding_hash=sha256_bytes(b"binding"),
        origin=ORIGIN,
        approval_identity_id="aid_00000000000000000000000001",
        allowed_credentials=[
            {
                "passkey_credential_id": CREDENTIAL_ID,
                "credential_id_hash": sha256_bytes(raw_id),
            }
        ],
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )


def service(value: HumanPresenceProof | None) -> HumanPresenceService:
    return HumanPresenceService(
        ScalarSession(value),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )


def test_risk_gate_and_resource_binding_are_deterministic_and_closed() -> None:
    assert requires_human_presence("new_agent_session")
    assert normalize_resource_binding("new_agent_session", BINDING) == BINDING
    with pytest.raises(ValueError):
        normalize_resource_binding(
            "new_agent_session",
            {"scopes": ["operator.refund.approve"], "expires_in_seconds": 900},
        )
    with pytest.raises(ValueError):
        normalize_resource_binding("new_agent_session", {**BINDING, "admin": True})


@pytest.mark.asyncio
async def test_redis_challenge_is_exact_account_bound_and_one_time() -> None:
    redis = ScriptRedis()
    store = RedisHumanPresenceStore(redis)
    state = HumanPresenceChallengeState(
        challenge_id="hpc_00000000000000000000000001",
        challenge="a" * 43,
        account_id=ACCOUNT_ID,
        session_id_hash=sha256_bytes(SESSION_ID.encode()),
        action_class="new_agent_session",
        resource_binding=BINDING,
        resource_binding_hash=sha256_bytes(b"binding"),
        origin=ORIGIN,
        approval_identity_id="aid_00000000000000000000000001",
        allowed_credentials=[
            {
                "passkey_credential_id": CREDENTIAL_ID,
                "credential_id_hash": sha256_bytes(b"credential"),
            }
        ],
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    await store.save(state, ttl_seconds=120)
    assert (await store.consume(state.challenge_id, account_id=ACCOUNT_ID)).origin == ORIGIN
    with pytest.raises(ChallengeAlreadyUsedError):
        await store.consume(state.challenge_id, account_id=ACCOUNT_ID)


@pytest.mark.asyncio
async def test_valid_user_verified_assertion_issues_safe_proof() -> None:
    raw_id = b"human-presence-credential"
    account = Account(
        id=ACCOUNT_ID, display_name="Buyer", status=AccountStatus.ACTIVE, session_version=1
    )
    credential = PasskeyCredential(
        id=CREDENTIAL_ID,
        approval_identity_id="aid_00000000000000000000000001",
        credential_id=raw_id,
        public_key=b"public-key",
        sign_count=1,
        transports=["internal"],
    )
    session = SequenceSession([account, credential])
    state = challenge_state(raw_id)
    human_presence = HumanPresenceService(
        session,  # type: ignore[arg-type]
        MemoryStore(state),  # type: ignore[arg-type]
        FakeWebAuthn(raw_id),  # type: ignore[arg-type]
        Settings(
            _env_file=None,
            database_url="postgresql://test:test@localhost/metergate",
            redis_url="redis://localhost:6379/15",
        ),
    )
    response = await human_presence.verify(
        state.challenge_id,
        HumanPresenceVerify(
            credential={
                "id": encode_base64url(raw_id),
                "rawId": encode_base64url(raw_id),
                "type": "public-key",
                "response": {},
                "clientExtensionResults": {},
            }
        ),
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
        origin=ORIGIN,
        account_session_version=1,
    )
    assert response.id.startswith("hpp_")
    assert response.state == "active"
    assert credential.sign_count == 2
    assert session.commits == 1
    persisted = next(item for item in session.added if isinstance(item, HumanPresenceProof))
    assert set(response.model_dump()).isdisjoint(
        {"session_id_hash", "challenge_hash", "passkey_credential_id", "credential"}
    )
    assert persisted.origin == ORIGIN


@pytest.mark.asyncio
async def test_missing_user_verification_and_disabled_account_fail_closed() -> None:
    raw_id = b"human-presence-credential"
    state = challenge_state(raw_id)
    credential = PasskeyCredential(
        id=CREDENTIAL_ID,
        approval_identity_id=state.approval_identity_id,
        credential_id=raw_id,
        public_key=b"public-key",
        sign_count=1,
        transports=["internal"],
    )
    payload = HumanPresenceVerify(
        credential={
            "id": encode_base64url(raw_id),
            "rawId": encode_base64url(raw_id),
            "type": "public-key",
            "response": {},
            "clientExtensionResults": {},
        }
    )
    settings = Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/metergate",
        redis_url="redis://localhost:6379/15",
    )
    disabled = Account(
        id=ACCOUNT_ID, display_name="Buyer", status=AccountStatus.DISABLED, session_version=1
    )
    with pytest.raises(AuthenticationForbiddenError):
        await HumanPresenceService(
            SequenceSession([disabled]),  # type: ignore[arg-type]
            MemoryStore(state),  # type: ignore[arg-type]
            FakeWebAuthn(raw_id),  # type: ignore[arg-type]
            settings,
        ).verify(
            state.challenge_id,
            payload,
            account_id=ACCOUNT_ID,
            session_id=SESSION_ID,
            origin=ORIGIN,
            account_session_version=1,
        )
    active = Account(
        id=ACCOUNT_ID, display_name="Buyer", status=AccountStatus.ACTIVE, session_version=1
    )
    with pytest.raises(AuthenticationVerificationError) as uv:
        await HumanPresenceService(
            SequenceSession([active, credential]),  # type: ignore[arg-type]
            MemoryStore(state),  # type: ignore[arg-type]
            FakeWebAuthn(raw_id, user_verified=False),  # type: ignore[arg-type]
            settings,
        ).verify(
            state.challenge_id,
            payload,
            account_id=ACCOUNT_ID,
            session_id=SESSION_ID,
            origin=ORIGIN,
            account_session_version=1,
        )
    assert getattr(uv.value, "reason_code", None) == "HUMAN_PRESENCE_USER_VERIFICATION_REQUIRED"


@pytest.mark.asyncio
async def test_exact_proof_consumes_once_and_rejects_cross_scope_replay() -> None:
    value = proof()
    human_presence = service(value)
    await human_presence.require_and_consume(
        PROOF_ID,
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
        origin=ORIGIN,
        action_class="new_agent_session",
        resource_binding=BINDING,
    )
    assert value.consumed_at is not None
    with pytest.raises(AuthenticationConflictError) as replay:
        await human_presence.require_and_consume(
            PROOF_ID,
            account_id=ACCOUNT_ID,
            session_id=SESSION_ID,
            origin=ORIGIN,
            action_class="new_agent_session",
            resource_binding=BINDING,
        )
    assert replay.value.reason_code == "HUMAN_PRESENCE_PROOF_REPLAYED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_id", "session_id", "origin", "binding"),
    [
        ("acct_00000000000000000000000002", SESSION_ID, ORIGIN, BINDING),
        (ACCOUNT_ID, "ses_" + "b" * 43, ORIGIN, BINDING),
        (ACCOUNT_ID, SESSION_ID, "https://evil.example", BINDING),
        (
            ACCOUNT_ID,
            SESSION_ID,
            ORIGIN,
            {"scopes": ["mcp:quote.create"], "expires_in_seconds": 900},
        ),
    ],
)
async def test_proof_rejects_wrong_account_session_origin_or_resource(
    account_id: str, session_id: str, origin: str, binding: dict[str, object]
) -> None:
    with pytest.raises(AuthenticationForbiddenError):
        await service(proof()).require_and_consume(
            PROOF_ID,
            account_id=account_id,
            session_id=session_id,
            origin=origin,
            action_class="new_agent_session",
            resource_binding=binding,
        )


@pytest.mark.asyncio
async def test_expired_and_tampered_proofs_fail_closed() -> None:
    with pytest.raises(AuthenticationExpiredError):
        await service(proof(expires_at=NOW - timedelta(seconds=1))).require_and_consume(
            PROOF_ID,
            account_id=ACCOUNT_ID,
            session_id=SESSION_ID,
            origin=ORIGIN,
            action_class="new_agent_session",
            resource_binding=BINDING,
        )
    tampered = proof()
    tampered.presence_hash = f"sha256:{'f' * 64}"
    with pytest.raises(AuthenticationForbiddenError) as error:
        await service(tampered).require_and_consume(
            PROOF_ID,
            account_id=ACCOUNT_ID,
            session_id=SESSION_ID,
            origin=ORIGIN,
            action_class="new_agent_session",
            resource_binding=BINDING,
        )
    assert error.value.reason_code == "HUMAN_PRESENCE_INTEGRITY_FAILED"


def test_mcp_surface_has_no_human_presence_minting_tool(make_client) -> None:
    paths = make_client().get("/openapi.json").json()["paths"]
    mcp_tools = {path for path in paths if path.startswith("/api/v1/mcp/tools/")}
    assert not any("human-presence" in path or "captcha" in path for path in mcp_tools)
