from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.api.v1.dependencies import (
    get_approval_application_service,
    get_passkey_application_service,
)
from app.domain.enums import ApprovalIdentityStatus, PurchaseType
from app.domain.exceptions import (
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalNotFoundError,
    ApprovalVerificationError,
)
from app.domain.policy_engine import PolicyCheckResult, PolicyReasonCode, PolicyRule
from app.schemas.approvals import (
    ApprovalAssertionVerify,
    ApprovalChallengeCreate,
    ApprovalChallengeResponse,
    ApprovalIdentityCreate,
    ApprovalIdentityResponse,
    ApprovalReview,
    ApprovalReviewParty,
    ApprovalReviewPolicyCheck,
    PasskeyCredentialResponse,
    PasskeyRegistrationOptionsResponse,
    PasskeyRegistrationResponse,
    PasskeyRegistrationVerify,
    PurchaseAuthorizationResponse,
)

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
IDENTITY_ID = "aid_00000000000000000000000001"
CREDENTIAL_ID = "pkc_00000000000000000000000001"
CHALLENGE_ID = "ach_00000000000000000000000001"
AUTHORIZATION_ID = "aut_00000000000000000000000001"
EVALUATION_ID = "pye_00000000000000000000000001"
POLICY_ID = "pol_00000000000000000000000001"
QUOTE_ID = "qte_00000000000000000000000001"
MERCHANT_ID = "mrc_00000000000000000000000001"
SERVICE_ID = "svc_00000000000000000000000001"
POLICY_HASH = f"sha256:{'1' * 64}"
QUOTE_HASH = f"sha256:{'2' * 64}"
REVIEW_HASH = f"sha256:{'3' * 64}"
AUTHORIZATION_HASH = f"sha256:{'4' * 64}"


def identity_response() -> ApprovalIdentityResponse:
    return ApprovalIdentityResponse(
        id=IDENTITY_ID,
        subject_ref="dev-user-001",
        display_name="Development User",
        status=ApprovalIdentityStatus.ACTIVE,
        credential_count=1,
        created_at=NOW,
        updated_at=NOW,
    )


def registration_options_response() -> PasskeyRegistrationOptionsResponse:
    return PasskeyRegistrationOptionsResponse(
        challenge_id=CHALLENGE_ID,
        public_key={
            "challenge": "opaque-challenge",
            "rp": {"id": "localhost", "name": "MeterGate"},
            "userVerification": "required",
        },
        expires_at=NOW + timedelta(minutes=5),
    )


def registration_response() -> PasskeyRegistrationResponse:
    return PasskeyRegistrationResponse(
        credential=PasskeyCredentialResponse(
            id=CREDENTIAL_ID,
            transports=["internal"],
            created_at=NOW,
            last_used_at=None,
        )
    )


def approval_review() -> ApprovalReview:
    checks = [
        ApprovalReviewPolicyCheck(
            rule=rule,
            result=PolicyCheckResult.PASS,
            reason_code=PolicyReasonCode.PASS_POLICY_INTEGRITY_VERIFIED,
            details={},
            explanation=f"{rule.value} passed",
        )
        for rule in PolicyRule
    ]
    return ApprovalReview(
        review_version="1",
        evaluation_id=EVALUATION_ID,
        evaluation_version="1",
        policy_id=POLICY_ID,
        policy_hash=POLICY_HASH,
        quote_id=QUOTE_ID,
        quote_hash=QUOTE_HASH,
        subject_ref="dev-user-001",
        approval_identity_id=IDENTITY_ID,
        merchant=ApprovalReviewParty(id=MERCHANT_ID, name="OrbitIntel"),
        service=ApprovalReviewParty(id=SERVICE_ID, name="Orbital Risk Report"),
        amount=500,
        currency="INR",
        purchase_type=PurchaseType.ONE_TIME,
        quote_expires_at="2026-08-25T12:05:00.000000Z",
        policy_expires_at="2026-08-25T12:15:00.000000Z",
        policy_checks=checks,
    )


def challenge_response() -> ApprovalChallengeResponse:
    return ApprovalChallengeResponse(
        challenge_id=CHALLENGE_ID,
        public_key={
            "challenge": "opaque-challenge",
            "rpId": "localhost",
            "userVerification": "required",
        },
        review=approval_review(),
        review_hash=REVIEW_HASH,
        expires_at=NOW + timedelta(minutes=5),
    )


def authorization_response() -> PurchaseAuthorizationResponse:
    return PurchaseAuthorizationResponse(
        id=AUTHORIZATION_ID,
        state="active",
        subject_ref="dev-user-001",
        merchant={"id": MERCHANT_ID, "name": "OrbitIntel"},
        service={"id": SERVICE_ID, "name": "Orbital Risk Report"},
        amount=500,
        currency="INR",
        purchase_type=PurchaseType.ONE_TIME,
        evaluation_id=EVALUATION_ID,
        policy_hash=POLICY_HASH,
        quote_hash=QUOTE_HASH,
        review_hash=REVIEW_HASH,
        authorized_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
        authorization_version="1",
        authorization_hash=AUTHORIZATION_HASH,
    )


class StubPasskeyService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.last_identity_payload: ApprovalIdentityCreate | None = None
        self.last_verification: tuple[str, PasskeyRegistrationVerify] | None = None

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def create_identity(
        self,
        payload: ApprovalIdentityCreate,
    ) -> ApprovalIdentityResponse:
        self._raise()
        self.last_identity_payload = payload
        return identity_response()

    async def get_identity(self, identity_id: str) -> ApprovalIdentityResponse:
        self._raise()
        assert identity_id == IDENTITY_ID
        return identity_response()

    async def registration_options(
        self,
        identity_id: str,
    ) -> PasskeyRegistrationOptionsResponse:
        self._raise()
        assert identity_id == IDENTITY_ID
        return registration_options_response()

    async def verify_registration(
        self,
        identity_id: str,
        payload: PasskeyRegistrationVerify,
    ) -> PasskeyRegistrationResponse:
        self._raise()
        self.last_verification = identity_id, payload
        return registration_response()


class StubApprovalService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.last_challenge_payload: ApprovalChallengeCreate | None = None
        self.last_assertion: tuple[str, ApprovalAssertionVerify] | None = None

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def create_challenge(
        self,
        payload: ApprovalChallengeCreate,
    ) -> ApprovalChallengeResponse:
        self._raise()
        self.last_challenge_payload = payload
        return challenge_response()

    async def verify_challenge(
        self,
        challenge_id: str,
        payload: ApprovalAssertionVerify,
    ) -> PurchaseAuthorizationResponse:
        self._raise()
        self.last_assertion = challenge_id, payload
        return authorization_response()

    async def get_authorization(
        self,
        authorization_id: str,
    ) -> PurchaseAuthorizationResponse:
        self._raise()
        assert authorization_id == AUTHORIZATION_ID
        return authorization_response()


def client_with_approval_services(
    make_client: Callable[..., TestClient],
    *,
    passkeys: StubPasskeyService | None = None,
    approvals: StubApprovalService | None = None,
) -> TestClient:
    client = make_client()
    client.app.dependency_overrides[get_passkey_application_service] = lambda: (
        passkeys or StubPasskeyService()
    )
    client.app.dependency_overrides[get_approval_application_service] = lambda: (
        approvals or StubApprovalService()
    )
    return client


def credential_json() -> dict[str, object]:
    return {
        "id": "Y3JlZGVudGlhbA",
        "rawId": "Y3JlZGVudGlhbA",
        "type": "public-key",
        "response": {
            "clientDataJSON": "Y2xpZW50",
            "authenticatorData": "YXV0aGVudGljYXRvcg",
            "signature": "c2lnbmF0dXJl",
        },
        "clientExtensionResults": {},
    }


def test_approval_routes_expose_only_append_only_methods(
    make_client: Callable[..., TestClient],
) -> None:
    client = client_with_approval_services(make_client)
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths["/api/v1/approval-identities"]) == {"post"}
    assert set(paths["/api/v1/approval-identities/{identity_id}"]) == {"get"}
    assert set(paths["/api/v1/approval-identities/{identity_id}/passkeys/options"]) == {"post"}
    assert set(paths["/api/v1/approval-identities/{identity_id}/passkeys/verify"]) == {"post"}
    assert set(paths["/api/v1/approval-challenges"]) == {"post"}
    assert set(paths["/api/v1/approval-challenges/{challenge_id}/verify"]) == {"post"}
    assert set(paths["/api/v1/authorizations/{authorization_id}"]) == {"get"}
    assert client.patch(f"/api/v1/authorizations/{AUTHORIZATION_ID}", json={}).status_code == 405
    assert client.delete(f"/api/v1/approval-identities/{IDENTITY_ID}").status_code == 405


def test_identity_and_registration_contracts_are_strict_and_safe(
    make_client: Callable[..., TestClient],
) -> None:
    passkeys = StubPasskeyService()
    client = client_with_approval_services(make_client, passkeys=passkeys)

    created = client.post(
        "/api/v1/approval-identities",
        json={"subject_ref": "dev-user-001", "display_name": "Development User"},
    )
    injected = client.post(
        "/api/v1/approval-identities",
        json={
            "subject_ref": "dev-user-001",
            "display_name": "Development User",
            "status": "active",
            "webauthn_user_handle": "secret",
        },
    )
    options = client.post(f"/api/v1/approval-identities/{IDENTITY_ID}/passkeys/options")
    verified = client.post(
        f"/api/v1/approval-identities/{IDENTITY_ID}/passkeys/verify",
        json={"challenge_id": CHALLENGE_ID, "credential": credential_json()},
    )

    assert created.status_code == 201
    assert injected.status_code == 422
    assert options.status_code == 200
    assert verified.status_code == 201
    combined = str(created.json()) + str(verified.json())
    assert "webauthn_user_handle" not in combined
    assert "public_key" not in combined
    assert "credential-id" not in combined
    assert "signature" not in combined


def test_approval_request_accepts_only_ids_and_response_is_server_derived(
    make_client: Callable[..., TestClient],
) -> None:
    approvals = StubApprovalService()
    client = client_with_approval_services(make_client, approvals=approvals)

    created = client.post(
        "/api/v1/approval-challenges",
        json={
            "evaluation_id": EVALUATION_ID,
            "approval_identity_id": IDENTITY_ID,
        },
    )
    injected = client.post(
        "/api/v1/approval-challenges",
        json={
            "evaluation_id": EVALUATION_ID,
            "approval_identity_id": IDENTITY_ID,
            "amount": 1,
            "decision": "allow",
            "review_hash": REVIEW_HASH,
        },
    )

    assert created.status_code == 201
    assert approvals.last_challenge_payload == ApprovalChallengeCreate(
        evaluation_id=EVALUATION_ID,
        approval_identity_id=IDENTITY_ID,
    )
    assert created.json()["review"]["amount"] == 500
    assert created.json()["review_hash"] == REVIEW_HASH
    assert injected.status_code == 422
    assert {issue["type"] for issue in injected.json()["detail"]} == {"extra_forbidden"}


def test_approval_verify_and_get_never_expose_sensitive_webauthn_material(
    make_client: Callable[..., TestClient],
) -> None:
    approvals = StubApprovalService()
    client = client_with_approval_services(make_client, approvals=approvals)

    verified = client.post(
        f"/api/v1/approval-challenges/{CHALLENGE_ID}/verify",
        json={"credential": credential_json()},
    )
    fetched = client.get(f"/api/v1/authorizations/{AUTHORIZATION_ID}")

    assert verified.status_code == 201
    assert fetched.status_code == 200
    for response in (verified, fetched):
        body = response.json()
        assert set(body).isdisjoint(
            {
                "passkey_credential_id",
                "credential_id",
                "public_key",
                "signature",
                "challenge",
                "challenge_hash",
            }
        )
        assert body["authorization_hash"] == AUTHORIZATION_HASH
        assert body["state"] == "active"


def test_approval_failures_have_stable_sanitized_http_mappings(
    make_client: Callable[..., TestClient],
) -> None:
    cases = [
        (
            ApprovalNotFoundError("Challenge not found", "APPROVAL_CHALLENGE_NOT_FOUND"),
            404,
        ),
        (
            ApprovalExpiredError("Challenge expired", "APPROVAL_CHALLENGE_EXPIRED"),
            410,
        ),
        (
            ApprovalVerificationError(
                "Approval failed",
                "APPROVAL_WEBAUTHN_VERIFICATION_FAILED",
            ),
            400,
        ),
        (
            ApprovalConflictError("Challenge used", "APPROVAL_CHALLENGE_ALREADY_USED"),
            409,
        ),
        (
            ApprovalIntegrityError(
                "Authorization integrity failed",
                "INTEGRITY_AUTHORIZATION_HASH_MISMATCH",
            ),
            500,
        ),
    ]

    for error, expected_status in cases:
        client = client_with_approval_services(
            make_client,
            approvals=StubApprovalService(error),
        )
        response = client.get(f"/api/v1/authorizations/{AUTHORIZATION_ID}")

        assert response.status_code == expected_status
        assert response.json() == {
            "detail": str(error),
            "reason_code": error.reason_code,
        }
