from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from app.api.v1.dependencies import (
    get_buyer_policy_application_service,
    get_policy_evaluation_application_service,
    require_authenticated_mutation,
    require_current_account,
)
from app.domain.enums import PolicyDecision
from app.domain.exceptions import (
    PolicyEvaluationIntegrityError,
    PolicyIntegrityError,
    PolicyTTLExceededError,
    ResourceNotFoundError,
)
from app.domain.policy_engine import (
    PolicyCheckResult,
    PolicyReasonCode,
    PolicyRule,
)
from app.schemas.policies import (
    BuyerPolicyCreate,
    BuyerPolicyResponse,
    PolicyConstraints,
)
from app.schemas.policy_evaluations import (
    PolicyCheckResponse,
    PolicyEvaluationCreate,
    PolicyEvaluationResponse,
)

POLICY_ID = "pol_00000000000000000000000001"
EVALUATION_ID = "pye_00000000000000000000000001"
QUOTE_ID = "qte_00000000000000000000000001"
MERCHANT_ID = "mrc_00000000000000000000000001"
SERVICE_ID = "svc_00000000000000000000000001"
ACCOUNT_ID = "acct_00000000000000000000000001"
IDENTITY_ID = "aid_00000000000000000000000001"
CREATED_AT = datetime(2026, 8, 25, 12, tzinfo=UTC)


def authenticated_context() -> Any:
    return SimpleNamespace(
        account=SimpleNamespace(id=ACCOUNT_ID),
        approval_identity=SimpleNamespace(id=IDENTITY_ID, subject_ref=ACCOUNT_ID),
    )


def policy_response() -> BuyerPolicyResponse:
    return BuyerPolicyResponse(
        id=POLICY_ID,
        subject_ref=ACCOUNT_ID,
        constraints=PolicyConstraints(
            maximum_amount=1000,
            allowed_currencies=["INR"],
            allowed_merchant_ids=[MERCHANT_ID],
            allowed_service_ids=[SERVICE_ID],
            allowed_service_types=["report"],
            allowed_purchase_types=["one_time"],
        ),
        issued_at=CREATED_AT,
        expires_at=CREATED_AT + timedelta(minutes=15),
        state="active",
        policy_version="1",
        policy_hash=f"sha256:{'1' * 64}",
        created_at=CREATED_AT,
    )


def evaluation_response(
    decision: PolicyDecision = PolicyDecision.ALLOW,
) -> PolicyEvaluationResponse:
    failed = decision is PolicyDecision.DENY
    reason_code = (
        PolicyReasonCode.DENY_AMOUNT_EXCEEDS_LIMIT
        if failed
        else PolicyReasonCode.PASS_AMOUNT_WITHIN_LIMIT
    )
    return PolicyEvaluationResponse(
        id=EVALUATION_ID,
        policy_id=POLICY_ID,
        quote_id=QUOTE_ID,
        policy_hash=f"sha256:{'1' * 64}",
        quote_hash=f"sha256:{'2' * 64}",
        decision=decision,
        reason_codes=(
            [PolicyReasonCode.DENY_AMOUNT_EXCEEDS_LIMIT]
            if failed
            else [PolicyReasonCode.ALLOW_POLICY_SATISFIED]
        ),
        checks=[
            PolicyCheckResponse(
                rule=PolicyRule.MAXIMUM_AMOUNT,
                result=(PolicyCheckResult.FAIL if failed else PolicyCheckResult.PASS),
                reason_code=reason_code,
                details={"actual": 500, "maximum": 100 if failed else 1000},
            )
        ],
        evaluated_at=CREATED_AT,
        evaluation_version="1",
        created_at=CREATED_AT,
    )


class StubPolicyApplicationService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.last_payload: BuyerPolicyCreate | None = None
        self.last_subject_ref: str | None = None
        self.last_owned_subject_refs: frozenset[str] | None = None

    async def create(
        self,
        payload: BuyerPolicyCreate,
        *,
        subject_ref: str,
    ) -> BuyerPolicyResponse:
        if self.error is not None:
            raise self.error
        self.last_payload = payload
        self.last_subject_ref = subject_ref
        return policy_response()

    async def get(
        self,
        policy_id: str,
        *,
        owned_subject_refs: frozenset[str],
    ) -> BuyerPolicyResponse:
        if self.error is not None:
            raise self.error
        assert policy_id == POLICY_ID
        self.last_owned_subject_refs = owned_subject_refs
        return policy_response()


class StubEvaluationApplicationService:
    def __init__(
        self,
        *,
        response: PolicyEvaluationResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or evaluation_response()
        self.error = error
        self.last_payload: PolicyEvaluationCreate | None = None
        self.last_owned_subject_refs: frozenset[str] | None = None

    async def create(
        self,
        payload: PolicyEvaluationCreate,
        *,
        owned_subject_refs: frozenset[str],
    ) -> PolicyEvaluationResponse:
        if self.error is not None:
            raise self.error
        self.last_payload = payload
        self.last_owned_subject_refs = owned_subject_refs
        return self.response

    async def get(
        self,
        evaluation_id: str,
        *,
        owned_subject_refs: frozenset[str],
    ) -> PolicyEvaluationResponse:
        if self.error is not None:
            raise self.error
        assert evaluation_id == EVALUATION_ID
        self.last_owned_subject_refs = owned_subject_refs
        return self.response


def client_with_policy_services(
    make_client: Callable[..., TestClient],
    policy_service: StubPolicyApplicationService | None = None,
    evaluation_service: StubEvaluationApplicationService | None = None,
) -> TestClient:
    client = make_client()
    client.app.dependency_overrides[get_buyer_policy_application_service] = lambda: (
        policy_service or StubPolicyApplicationService()
    )
    client.app.dependency_overrides[get_policy_evaluation_application_service] = lambda: (
        evaluation_service or StubEvaluationApplicationService()
    )
    current = authenticated_context()
    client.app.dependency_overrides[require_current_account] = lambda: current
    client.app.dependency_overrides[require_authenticated_mutation] = lambda: current
    return client


def policy_request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "maximum_amount": 1000,
        "expires_in_seconds": 900,
    }
    payload.update(overrides)
    return payload


def test_policy_routes_are_append_only_and_expose_exact_methods(
    make_client: Callable[..., TestClient],
) -> None:
    client = client_with_policy_services(make_client)
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths["/api/v1/policies"]) == {"post"}
    assert set(paths["/api/v1/policies/{policy_id}"]) == {"get"}
    assert set(paths["/api/v1/policy-evaluations"]) == {"post"}
    assert set(paths["/api/v1/policy-evaluations/{evaluation_id}"]) == {"get"}
    assert client.patch(f"/api/v1/policies/{POLICY_ID}", json={}).status_code == 405
    assert client.delete(f"/api/v1/policy-evaluations/{EVALUATION_ID}").status_code == 405


def test_policy_create_omits_allowlists_as_unconstrained_and_returns_created_at(
    make_client: Callable[..., TestClient],
) -> None:
    application = StubPolicyApplicationService()
    client = client_with_policy_services(make_client, policy_service=application)

    response = client.post("/api/v1/policies", json=policy_request())

    assert response.status_code == 201
    assert application.last_payload is not None
    assert application.last_payload.allowed_currencies is None
    assert application.last_payload.allowed_merchant_ids is None
    assert application.last_payload.allowed_service_ids is None
    assert application.last_payload.allowed_service_types is None
    assert application.last_payload.allowed_purchase_types is None
    assert application.last_subject_ref == ACCOUNT_ID
    assert response.json()["created_at"] == "2026-08-25T12:00:00Z"


def test_policy_create_sorts_allowlists_and_rejects_duplicates_or_server_fields(
    make_client: Callable[..., TestClient],
) -> None:
    application = StubPolicyApplicationService()
    client = client_with_policy_services(make_client, policy_service=application)

    created = client.post(
        "/api/v1/policies",
        json=policy_request(allowed_currencies=["USD", "INR"]),
    )
    duplicate = client.post(
        "/api/v1/policies",
        json=policy_request(allowed_currencies=["INR", "INR"]),
    )
    injected = client.post(
        "/api/v1/policies",
        json=policy_request(
            subject_ref="acct_00000000000000000000000002",
            policy_hash=f"sha256:{'0' * 64}",
            decision="allow",
        ),
    )

    assert created.status_code == 201
    assert application.last_payload is not None
    assert application.last_payload.allowed_currencies == ["INR", "USD"]
    assert duplicate.status_code == 422
    assert injected.status_code == 422
    assert {issue["type"] for issue in injected.json()["detail"]} == {"extra_forbidden"}


def test_configured_policy_ttl_failure_has_a_stable_sanitized_422(
    make_client: Callable[..., TestClient],
) -> None:
    client = client_with_policy_services(
        make_client,
        policy_service=StubPolicyApplicationService(error=PolicyTTLExceededError(300)),
    )

    response = client.post("/api/v1/policies", json=policy_request())

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "less_than_equal",
                "loc": ["body", "expires_in_seconds"],
                "msg": "Policy lifetime exceeds the configured maximum",
                "ctx": {"le": 300},
            }
        ]
    }


def test_evaluation_request_is_exact_and_denial_evidence_is_returned(
    make_client: Callable[..., TestClient],
) -> None:
    application = StubEvaluationApplicationService(
        response=evaluation_response(PolicyDecision.DENY)
    )
    client = client_with_policy_services(make_client, evaluation_service=application)

    created = client.post(
        "/api/v1/policy-evaluations",
        json={"policy_id": POLICY_ID, "quote_id": QUOTE_ID},
    )
    injected = client.post(
        "/api/v1/policy-evaluations",
        json={
            "policy_id": POLICY_ID,
            "quote_id": QUOTE_ID,
            "decision": "allow",
            "amount": 1,
        },
    )

    assert created.status_code == 201
    assert application.last_payload == PolicyEvaluationCreate(
        policy_id=POLICY_ID,
        quote_id=QUOTE_ID,
    )
    assert application.last_owned_subject_refs == frozenset({ACCOUNT_ID})
    assert created.json()["decision"] == "deny"
    assert created.json()["reason_codes"] == ["DENY_AMOUNT_EXCEEDS_LIMIT"]
    assert created.json()["evaluation_version"] == "1"
    assert created.json()["created_at"] == "2026-08-25T12:00:00Z"
    assert injected.status_code == 422
    assert {issue["type"] for issue in injected.json()["detail"]} == {"extra_forbidden"}


def test_unknown_records_and_integrity_failures_have_safe_stable_mappings(
    make_client: Callable[..., TestClient],
) -> None:
    missing = client_with_policy_services(
        make_client,
        policy_service=StubPolicyApplicationService(
            error=ResourceNotFoundError("Buyer policy", POLICY_ID)
        ),
    ).get(f"/api/v1/policies/{POLICY_ID}")
    policy_integrity = client_with_policy_services(
        make_client,
        policy_service=StubPolicyApplicationService(error=PolicyIntegrityError(POLICY_ID)),
    ).get(f"/api/v1/policies/{POLICY_ID}")
    evaluation_integrity = client_with_policy_services(
        make_client,
        evaluation_service=StubEvaluationApplicationService(
            error=PolicyEvaluationIntegrityError(EVALUATION_ID)
        ),
    ).get(f"/api/v1/policy-evaluations/{EVALUATION_ID}")

    assert missing.status_code == 404
    assert missing.json() == {"detail": f"Buyer policy '{POLICY_ID}' was not found"}
    assert policy_integrity.status_code == 500
    assert policy_integrity.json()["reason_code"] == "INTEGRITY_POLICY_HASH_MISMATCH"
    assert evaluation_integrity.status_code == 500
    assert evaluation_integrity.json()["reason_code"] == "INTEGRITY_POLICY_EVALUATION_DATA_INVALID"
