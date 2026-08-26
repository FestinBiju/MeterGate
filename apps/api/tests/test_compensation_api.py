from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.v1.dependencies import (
    get_compensation_application_service,
    get_refund_application_service,
    require_authenticated_mutation,
    require_current_account,
)
from app.domain.enums import PaymentProvider, PaymentRefundState
from app.domain.exceptions import (
    CompensationForbiddenError,
    CompensationIntegrityError,
    CompensationNotFoundError,
)
from app.models import PaymentRefund
from app.schemas.compensations import CompensationCaseResponse, PaymentRefundResponse

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)
ACCOUNT_ID = "acct_00000000000000000000000001"
TRANSACTION_ID = "txn_00000000000000000000000001"
COMPENSATION_ID = "cmp_00000000000000000000000001"
REFUND_ID = "rfd_00000000000000000000000001"
ATTEMPT_ID = "pmt_00000000000000000000000001"
PAYMENT_ID = "pay_testPayment123"
PROVIDER_REFUND_ID = "rfnd_testRefund123"
ENTITLEMENT_ID = "ent_00000000000000000000000001"
FULFILLMENT_ID = "ful_00000000000000000000000001"
MERCHANT_ID = "mrc_00000000000000000000000001"
SERVICE_ID = "svc_00000000000000000000000001"
FAILURE_HASH = "sha256:" + "a" * 64


def refund_response() -> PaymentRefundResponse:
    return PaymentRefundResponse(
        reason_code="PAYMENT_REFUND_FOUND",
        refund_id=REFUND_ID,
        compensation_id=COMPENSATION_ID,
        transaction_id=TRANSACTION_ID,
        payment_attempt_id=ATTEMPT_ID,
        provider="razorpay",
        provider_payment_id=PAYMENT_ID,
        provider_refund_id=PROVIDER_REFUND_ID,
        provider_receipt=REFUND_ID,
        amount=500,
        currency="INR",
        state="refunded",
        provider_status="processed",
        created_at=NOW,
        requested_at=NOW,
        processed_at=NOW,
        failed_at=None,
        last_reconciled_at=NOW,
        revision=3,
    )


def compensation_response() -> CompensationCaseResponse:
    refund = refund_response()
    return CompensationCaseResponse(
        reason_code="COMPENSATION_CASE_FOUND",
        compensation_id=COMPENSATION_ID,
        transaction_id=TRANSACTION_ID,
        payment_attempt_id=ATTEMPT_ID,
        provider_payment_id=PAYMENT_ID,
        entitlement_id=ENTITLEMENT_ID,
        fulfillment_execution_id=FULFILLMENT_ID,
        merchant_id=MERCHANT_ID,
        service_id=SERVICE_ID,
        amount_paid=500,
        currency="INR",
        failure_code="FULFILLMENT_PROVIDER_PERMANENT_FAILURE",
        failure_evidence_hash=FAILURE_HASH,
        recommended_action="full_refund",
        decision_state="completed",
        decision_provenance="automatic_approved",
        approved_refund_amount=500,
        decision_reason_code="COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED",
        created_at=NOW,
        decided_at=NOW,
        closed_at=NOW,
        refund={
            "refund_id": refund.refund_id,
            "provider_payment_id": refund.provider_payment_id,
            "provider_refund_id": refund.provider_refund_id,
            "amount": refund.amount,
            "currency": refund.currency,
            "state": refund.state,
            "provider_status": refund.provider_status,
        },
    )


class StubCompensationReads:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def get_for_transaction(
        self,
        transaction_id: str,
        *,
        account_id: str,
    ) -> CompensationCaseResponse:
        self._raise()
        self.calls.append(("transaction", transaction_id, account_id))
        return compensation_response()

    async def get_case(
        self,
        compensation_id: str,
        *,
        account_id: str,
    ) -> CompensationCaseResponse:
        self._raise()
        self.calls.append(("compensation", compensation_id, account_id))
        return compensation_response()

    async def get_refund(
        self,
        refund_id: str,
        *,
        account_id: str,
    ) -> PaymentRefundResponse:
        self._raise()
        self.calls.append(("refund", refund_id, account_id))
        return refund_response()


class StubRefundReconciliation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def reconcile_refund(
        self,
        refund_id: str,
        *,
        account_id: str,
    ) -> PaymentRefund:
        self.calls.append((refund_id, account_id))
        return PaymentRefund(
            id=REFUND_ID,
            compensation_case_id=COMPENSATION_ID,
            transaction_id=TRANSACTION_ID,
            payment_attempt_id=ATTEMPT_ID,
            provider=PaymentProvider.RAZORPAY,
            provider_payment_id=PAYMENT_ID,
            provider_refund_id=PROVIDER_REFUND_ID,
            provider_receipt=REFUND_ID,
            amount=500,
            currency="INR",
            refund_state=PaymentRefundState.REFUNDED,
            provider_status="processed",
            created_at=NOW,
            requested_at=NOW,
            processed_at=NOW,
            failed_at=None,
            last_reconciled_at=NOW,
            revision=3,
        )


def client_with_reads(
    make_client: Callable[..., TestClient],
    service: StubCompensationReads | None = None,
) -> tuple[TestClient, StubCompensationReads]:
    client = make_client()
    reads = service or StubCompensationReads()
    principal = lambda: SimpleNamespace(account=SimpleNamespace(id=ACCOUNT_ID))  # noqa: E731
    client.app.dependency_overrides[get_compensation_application_service] = lambda: reads
    client.app.dependency_overrides[require_current_account] = principal
    client.app.dependency_overrides[require_authenticated_mutation] = principal
    return client, reads


def test_compensation_routes_expose_only_reads_and_explicit_reconciliation(
    make_client: Callable[..., TestClient],
) -> None:
    client, _ = client_with_reads(make_client)
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths["/api/v1/payment-transactions/{transaction_id}/compensation"]) == {"get"}
    assert set(paths["/api/v1/compensations/{compensation_id}"]) == {"get"}
    assert set(paths["/api/v1/refunds/{refund_id}"]) == {"get"}
    assert set(paths["/api/v1/refunds/{refund_id}/reconcile"]) == {"post"}
    assert client.post(f"/api/v1/refunds/{REFUND_ID}").status_code == 405


def test_compensation_reads_forward_only_path_identity_and_authenticated_owner(
    make_client: Callable[..., TestClient],
) -> None:
    client, service = client_with_reads(make_client)

    by_transaction = client.get(f"/api/v1/payment-transactions/{TRANSACTION_ID}/compensation")
    by_case = client.get(f"/api/v1/compensations/{COMPENSATION_ID}")
    refund = client.get(f"/api/v1/refunds/{REFUND_ID}")

    assert [by_transaction.status_code, by_case.status_code, refund.status_code] == [200, 200, 200]
    assert all(
        response.headers["cache-control"] == "private, no-store"
        for response in (by_transaction, by_case, refund)
    )
    assert by_transaction.json()["compensation_id"] == COMPENSATION_ID
    assert by_case.json()["refund"]["state"] == "refunded"
    assert refund.json()["provider_refund_id"] == PROVIDER_REFUND_ID
    assert service.calls == [
        ("transaction", TRANSACTION_ID, ACCOUNT_ID),
        ("compensation", COMPENSATION_ID, ACCOUNT_ID),
        ("refund", REFUND_ID, ACCOUNT_ID),
    ]


def test_refund_reconciliation_uses_authenticated_owner_and_returns_current_evidence(
    make_client: Callable[..., TestClient],
) -> None:
    client, _ = client_with_reads(make_client)
    service = StubRefundReconciliation()
    client.app.dependency_overrides[get_refund_application_service] = lambda: service

    response = client.post(f"/api/v1/refunds/{REFUND_ID}/reconcile")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["reason_code"] == "REFUND_COMPLETED"
    assert response.json()["provider_refund_id"] == PROVIDER_REFUND_ID
    assert service.calls == [(REFUND_ID, ACCOUNT_ID)]


def test_compensation_reads_require_session_before_service_work(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()
    service = StubCompensationReads()
    refund_service = StubRefundReconciliation()
    client.app.dependency_overrides[get_compensation_application_service] = lambda: service
    client.app.dependency_overrides[get_refund_application_service] = lambda: refund_service

    responses = [
        client.get(f"/api/v1/payment-transactions/{TRANSACTION_ID}/compensation"),
        client.get(f"/api/v1/compensations/{COMPENSATION_ID}"),
        client.get(f"/api/v1/refunds/{REFUND_ID}"),
        client.post(
            f"/api/v1/refunds/{REFUND_ID}/reconcile",
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": "c" * 43},
        ),
    ]

    assert all(response.status_code == 401 for response in responses)
    assert all(response.headers["cache-control"] == "private, no-store" for response in responses)
    assert service.calls == []
    assert refund_service.calls == []


def test_compensation_route_ids_are_strict(
    make_client: Callable[..., TestClient],
) -> None:
    client, service = client_with_reads(make_client)

    assert (
        client.get("/api/v1/payment-transactions/not-a-transaction/compensation").status_code == 422
    )
    assert client.get("/api/v1/compensations/not-a-case").status_code == 422
    assert client.get("/api/v1/refunds/not-a-refund").status_code == 422
    assert client.post("/api/v1/refunds/not-a-refund/reconcile").status_code == 422
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (CompensationNotFoundError("missing", "COMPENSATION_CASE_NOT_FOUND"), 404),
        (CompensationForbiddenError("forbidden", "COMPENSATION_OWNERSHIP_MISMATCH"), 403),
        (CompensationIntegrityError("invalid", "COMPENSATION_INTEGRITY_FAILED"), 500),
    ],
)
def test_compensation_failures_use_sanitized_domain_mapping(
    make_client: Callable[..., TestClient],
    error: Exception,
    status_code: int,
) -> None:
    client, _ = client_with_reads(make_client, StubCompensationReads(error))

    response = client.get(f"/api/v1/compensations/{COMPENSATION_ID}")

    assert response.status_code == status_code
    assert response.headers["cache-control"] == "private, no-store"
    assert set(response.json()) == {"detail", "reason_code"}
    assert "Traceback" not in response.text


def test_pending_compensation_schema_does_not_fabricate_decision_evidence() -> None:
    pending = CompensationCaseResponse(
        reason_code="COMPENSATION_CASE_FOUND",
        compensation_id=COMPENSATION_ID,
        transaction_id=TRANSACTION_ID,
        payment_attempt_id=ATTEMPT_ID,
        provider_payment_id=PAYMENT_ID,
        entitlement_id=ENTITLEMENT_ID,
        fulfillment_execution_id=FULFILLMENT_ID,
        merchant_id=MERCHANT_ID,
        service_id=SERVICE_ID,
        amount_paid=500,
        currency="INR",
        failure_code="FULFILLMENT_PROVIDER_PERMANENT_FAILURE",
        failure_evidence_hash=FAILURE_HASH,
        recommended_action="manual_review",
        decision_state="pending",
        decision_provenance=None,
        approved_refund_amount=None,
        decision_reason_code=None,
        created_at=NOW,
        decided_at=None,
        closed_at=None,
        refund=None,
    )

    assert pending.decision_provenance is None
    assert pending.decision_reason_code is None
