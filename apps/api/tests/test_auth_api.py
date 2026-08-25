from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1.dependencies import (
    get_authentication_application_service,
    get_passkey_application_service,
)
from app.domain.enums import AccountStatus, ApprovalIdentityStatus
from app.domain.exceptions import AuthenticationUnauthorizedError
from app.repositories.accounts import AccountRepository
from app.schemas.auth import (
    AccountResponse,
    AuthCeremonyOptionsResponse,
    AuthLogoutResponse,
    AuthPasskeyListResponse,
    AuthPasskeyResponse,
    AuthSessionResponse,
    SessionApprovalIdentityResponse,
)
from app.services.auth import IssuedAuthSession

ACCOUNT_ID = "acct_00000000000000000000000001"
IDENTITY_ID = "aid_00000000000000000000000001"
CREDENTIAL_ID = "pkc_00000000000000000000000001"
CHALLENGE_ID = "ach_00000000000000000000000001"
SESSION_ID = f"ses_{'s' * 43}"
CSRF_TOKEN = "c" * 43
ORIGIN = "http://localhost:3000"


@pytest.fixture(autouse=True)
def stub_mutation_account_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_for_update(
        repository: AccountRepository,
        account_id: str,
    ) -> Any:
        del repository
        assert account_id == ACCOUNT_ID
        return SimpleNamespace(
            id=ACCOUNT_ID,
            status=AccountStatus.ACTIVE,
            session_version=1,
        )

    monkeypatch.setattr(AccountRepository, "get_for_update", get_for_update)


def _session_response(*, authenticated_at: datetime | None = None) -> AuthSessionResponse:
    now = authenticated_at or datetime.now(UTC)
    return AuthSessionResponse(
        account=AccountResponse(
            id=ACCOUNT_ID,
            display_name="Test Buyer",
            status=AccountStatus.ACTIVE,
            created_at=now - timedelta(minutes=1),
            updated_at=now - timedelta(minutes=1),
        ),
        approval_identity=SessionApprovalIdentityResponse(
            id=IDENTITY_ID,
            status=ApprovalIdentityStatus.ACTIVE,
            credential_count=1,
        ),
        authenticated_at=now,
        expires_at=now + timedelta(hours=1),
        csrf_token=CSRF_TOKEN,
    )


def _resolved_session(*, authenticated_at: datetime | None = None) -> Any:
    response = _session_response(authenticated_at=authenticated_at)
    return SimpleNamespace(
        state=SimpleNamespace(
            session_id=SESSION_ID,
            account_id=ACCOUNT_ID,
            approval_identity_id=IDENTITY_ID,
            passkey_credential_id=CREDENTIAL_ID,
            account_session_version=1,
            csrf_token=CSRF_TOKEN,
            authenticated_at=response.authenticated_at,
        ),
        account=SimpleNamespace(id=ACCOUNT_ID),
        approval_identity=SimpleNamespace(
            id=IDENTITY_ID,
            subject_ref=ACCOUNT_ID,
        ),
        response=response,
    )


class StubAuthenticationService:
    def __init__(self, *, resolved: Any | None = None, error: Exception | None = None) -> None:
        self.resolved = resolved or _resolved_session()
        self.error = error
        self.resolved_session_ids: list[str] = []
        self.revoked_session_ids: list[str] = []

    async def signup_options(self, payload: Any) -> AuthCeremonyOptionsResponse:
        assert payload.display_name == "Test Buyer"
        return self._options()

    async def signup_verify(self, payload: Any) -> IssuedAuthSession:
        assert payload.challenge_id == CHALLENGE_ID
        return IssuedAuthSession(session_id=SESSION_ID, response=self.resolved.response)

    async def login_options(self) -> AuthCeremonyOptionsResponse:
        return self._options()

    async def login_verify(self, payload: Any) -> IssuedAuthSession:
        assert payload.challenge_id == CHALLENGE_ID
        return IssuedAuthSession(session_id=SESSION_ID, response=self.resolved.response)

    async def resolve_session(self, session_id: str) -> Any:
        self.resolved_session_ids.append(session_id)
        if self.error is not None:
            raise self.error
        return self.resolved

    async def logout(self, session_id: str) -> AuthLogoutResponse:
        self.revoked_session_ids.append(session_id)
        return AuthLogoutResponse()

    async def list_passkeys(self, resolved: Any) -> AuthPasskeyListResponse:
        assert resolved is self.resolved
        return AuthPasskeyListResponse(
            credentials=[
                AuthPasskeyResponse(
                    id=CREDENTIAL_ID,
                    transports=["internal"],
                    created_at=datetime.now(UTC) - timedelta(days=1),
                    last_used_at=datetime.now(UTC),
                    current=True,
                )
            ]
        )

    @staticmethod
    def _options() -> AuthCeremonyOptionsResponse:
        return AuthCeremonyOptionsResponse(
            challenge_id=CHALLENGE_ID,
            public_key={"challenge": "Y2hhbGxlbmdl", "userVerification": "required"},
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def _credential_json() -> dict[str, object]:
    return {
        "id": "Y3JlZGVudGlhbA",
        "rawId": "Y3JlZGVudGlhbA",
        "type": "public-key",
        "response": {
            "clientDataJSON": "Y2xpZW50",
            "attestationObject": "YXR0ZXN0YXRpb24",
        },
        "clientExtensionResults": {},
    }


def _client(
    make_client: Callable[..., TestClient],
    service: StubAuthenticationService | None = None,
) -> tuple[TestClient, StubAuthenticationService]:
    client = make_client()
    auth = service or StubAuthenticationService()
    client.app.dependency_overrides[get_authentication_application_service] = lambda: auth
    return client, auth


def _authenticate(client: TestClient) -> None:
    client.cookies.set("metergate_session", SESSION_ID, path="/api/v1")


def test_auth_routes_are_explicit_and_never_expose_the_session_id(
    make_client: Callable[..., TestClient],
) -> None:
    client, _ = _client(make_client)
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths["/api/v1/auth/signup/options"]) == {"post"}
    assert set(paths["/api/v1/auth/signup/verify"]) == {"post"}
    assert set(paths["/api/v1/auth/login/options"]) == {"post"}
    assert set(paths["/api/v1/auth/login/verify"]) == {"post"}
    assert set(paths["/api/v1/auth/session"]) == {"get"}
    assert set(paths["/api/v1/auth/logout"]) == {"post"}
    assert set(paths["/api/v1/auth/passkeys"]) == {"get"}

    response = client.post(
        "/api/v1/auth/signup/verify",
        headers={"Origin": ORIGIN},
        json={"challenge_id": CHALLENGE_ID, "credential": _credential_json()},
    )

    assert response.status_code == 200
    assert SESSION_ID not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["reason_code"] == "AUTH_LOGGED_IN"
    assert response.json()["auth_method"] == "passkey"


def test_signup_and_login_ceremonies_fail_closed_without_an_allowed_origin(
    make_client: Callable[..., TestClient],
) -> None:
    client, _ = _client(make_client)

    missing = client.post(
        "/api/v1/auth/signup/options",
        json={"display_name": "Test Buyer"},
    )
    wrong = client.post(
        "/api/v1/auth/login/options",
        headers={"Origin": "https://attacker.example"},
        json={},
    )
    allowed = client.post(
        "/api/v1/auth/login/options",
        headers={"Origin": ORIGIN},
        json={},
    )

    assert missing.status_code == 403
    assert missing.json()["reason_code"] == "AUTH_ORIGIN_NOT_ALLOWED"
    assert wrong.status_code == 403
    assert wrong.json()["reason_code"] == "AUTH_ORIGIN_NOT_ALLOWED"
    assert allowed.status_code == 200


def test_verified_auth_sets_a_finite_httponly_cookie_with_configured_scope(
    make_client: Callable[..., TestClient],
) -> None:
    client, _ = _client(make_client)

    response = client.post(
        "/api/v1/auth/login/verify",
        headers={"Origin": ORIGIN},
        json={"challenge_id": CHALLENGE_ID, "credential": _credential_json()},
    )

    cookie = response.headers["set-cookie"]
    assert response.status_code == 200
    assert "metergate_session=" in cookie
    assert "HttpOnly" in cookie
    assert "Max-Age=3600" in cookie
    assert "Path=/api/v1" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie


def test_session_and_passkey_metadata_are_safe_gets_without_csrf(
    make_client: Callable[..., TestClient],
) -> None:
    client, auth = _client(make_client)
    unauthenticated = client.get("/api/v1/auth/session")
    _authenticate(client)

    session = client.get("/api/v1/auth/session")
    passkeys = client.get("/api/v1/auth/passkeys")
    hostile_origin = client.get(
        "/api/v1/auth/session",
        headers={"Origin": "https://attacker.example"},
    )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["reason_code"] == "AUTH_SESSION_REQUIRED"
    assert session.status_code == 200
    assert session.headers["cache-control"] == "private, no-store"
    assert session.headers["pragma"] == "no-cache"
    assert session.json()["account"]["id"] == ACCOUNT_ID
    assert passkeys.status_code == 200
    assert passkeys.json()["credentials"] == [
        {
            "id": CREDENTIAL_ID,
            "transports": ["internal"],
            "created_at": passkeys.json()["credentials"][0]["created_at"],
            "last_used_at": passkeys.json()["credentials"][0]["last_used_at"],
            "current": True,
        }
    ]
    assert "credential_id" not in passkeys.text
    assert "public_key" not in passkeys.text
    assert "sign_count" not in passkeys.text
    assert hostile_origin.status_code == 403
    assert hostile_origin.json()["reason_code"] == "AUTH_ORIGIN_NOT_ALLOWED"
    assert auth.resolved_session_ids == [SESSION_ID, SESSION_ID]


def test_logout_requires_origin_and_synchronizer_token_then_revokes_and_clears_cookie(
    make_client: Callable[..., TestClient],
) -> None:
    client, auth = _client(make_client)
    _authenticate(client)

    wrong_origin = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "https://attacker.example", "X-CSRF-Token": CSRF_TOKEN},
    )
    missing_csrf = client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
    wrong_csrf = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": "x" * 43},
    )
    valid = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF_TOKEN},
    )

    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["reason_code"] == "AUTH_ORIGIN_NOT_ALLOWED"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["reason_code"] == "AUTH_CSRF_REQUIRED"
    assert wrong_csrf.status_code == 403
    assert wrong_csrf.json()["reason_code"] == "AUTH_CSRF_INVALID"
    assert valid.status_code == 200
    assert valid.json() == {"reason_code": "AUTH_LOGGED_OUT"}
    assert auth.revoked_session_ids == [SESSION_ID]
    assert "Max-Age=0" in valid.headers["set-cookie"]


def test_session_resolution_failure_keeps_stable_401_code(
    make_client: Callable[..., TestClient],
) -> None:
    client, _ = _client(
        make_client,
        StubAuthenticationService(
            error=AuthenticationUnauthorizedError(
                "Authentication session has expired",
                "AUTH_SESSION_EXPIRED",
            )
        ),
    )
    _authenticate(client)

    response = client.get("/api/v1/auth/session")

    assert response.status_code == 401
    assert response.json() == {
        "detail": "Authentication session has expired",
        "reason_code": "AUTH_SESSION_EXPIRED",
    }


def test_additional_passkey_enrollment_requires_a_recent_authenticated_session(
    make_client: Callable[..., TestClient],
) -> None:
    stale = _resolved_session(authenticated_at=datetime.now(UTC) - timedelta(minutes=10))
    client, _ = _client(make_client, StubAuthenticationService(resolved=stale))
    client.app.dependency_overrides[get_passkey_application_service] = lambda: SimpleNamespace()

    unauthenticated = client.post(
        f"/api/v1/approval-identities/{IDENTITY_ID}/passkeys/options",
        headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF_TOKEN},
    )
    _authenticate(client)
    stale_auth = client.post(
        f"/api/v1/approval-identities/{IDENTITY_ID}/passkeys/options",
        headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF_TOKEN},
    )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["reason_code"] == "AUTH_SESSION_REQUIRED"
    assert stale_auth.status_code == 403
    assert stale_auth.json()["reason_code"] == "AUTH_REAUTH_REQUIRED"


@pytest.mark.parametrize(
    ("status", "session_version", "reason_code"),
    [
        (AccountStatus.DISABLED, 2, "AUTH_SESSION_INVALID"),
        (AccountStatus.DISABLED, 1, "AUTH_ACCOUNT_DISABLED"),
    ],
)
def test_mutation_relocks_account_and_rechecks_disable_or_session_version(
    make_client: Callable[..., TestClient],
    monkeypatch: pytest.MonkeyPatch,
    status: AccountStatus,
    session_version: int,
    reason_code: str,
) -> None:
    async def changed_account(
        repository: AccountRepository,
        account_id: str,
    ) -> Any:
        del repository
        assert account_id == ACCOUNT_ID
        return SimpleNamespace(
            id=ACCOUNT_ID,
            status=status,
            session_version=session_version,
        )

    monkeypatch.setattr(AccountRepository, "get_for_update", changed_account)
    client, auth = _client(make_client)
    _authenticate(client)

    response = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF_TOKEN},
    )

    assert response.status_code == 401
    assert response.json()["reason_code"] == reason_code
    assert auth.revoked_session_ids == []
