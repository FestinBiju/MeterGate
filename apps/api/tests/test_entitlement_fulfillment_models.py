from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from app.domain.entitlement_hashing import calculate_entitlement_hash
from app.domain.enums import (
    FulfillmentEventActorType,
    FulfillmentEventType,
    FulfillmentExecutionState,
    FulfillmentProviderType,
    PurchaseType,
)
from app.domain.hashing import sha256_json
from app.models import Base, Entitlement, FulfillmentEvent, FulfillmentExecution

HASH = "sha256:" + "1" * 64


def entitlement_for_test() -> Entitlement:
    issued_at = datetime.now(UTC) - timedelta(minutes=1)
    input_value = {"norad_id": 25544}
    input_hash = sha256_json(input_value)
    fields = {
        "entitlement_id": "ent_00000000000000000000000000",
        "account_id": "acct_00000000000000000000000000",
        "transaction_id": "txn_00000000000000000000000000",
        "payment_binding_hash": HASH,
        "payment_reverification_event_id": "pte_00000000000000000000000000",
        "payment_reverification_revision": 7,
        "provider_order_id": "order_exact",
        "provider_payment_id": "pay_exact",
        "authorization_id": "aut_00000000000000000000000000",
        "authorization_hash": HASH,
        "evaluation_id": "pye_00000000000000000000000000",
        "policy_id": "pol_00000000000000000000000000",
        "policy_hash": HASH,
        "quote_id": "qte_00000000000000000000000000",
        "quote_hash": HASH,
        "merchant_id": "mrc_00000000000000000000000000",
        "service_id": "svc_00000000000000000000000000",
        "input_value": input_value,
        "input_hash": input_hash,
        "amount": 900,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "maximum_executions": 1,
        "issued_at": issued_at,
        "expires_at": issued_at + timedelta(minutes=10),
        "entitlement_version": "1",
    }
    return Entitlement(
        id=fields["entitlement_id"],
        account_id=fields["account_id"],
        transaction_id=fields["transaction_id"],
        payment_binding_hash=fields["payment_binding_hash"],
        payment_reverification_event_id=fields["payment_reverification_event_id"],
        payment_reverification_revision=fields["payment_reverification_revision"],
        provider_order_id=fields["provider_order_id"],
        provider_payment_id=fields["provider_payment_id"],
        authorization_id=fields["authorization_id"],
        authorization_hash=fields["authorization_hash"],
        evaluation_id=fields["evaluation_id"],
        policy_id=fields["policy_id"],
        policy_hash=fields["policy_hash"],
        quote_id=fields["quote_id"],
        quote_hash=fields["quote_hash"],
        merchant_id=fields["merchant_id"],
        service_id=fields["service_id"],
        input=input_value,
        input_hash=input_hash,
        amount=fields["amount"],
        currency=fields["currency"],
        purchase_type=fields["purchase_type"],
        maximum_executions=fields["maximum_executions"],
        issued_at=issued_at,
        expires_at=fields["expires_at"],
        entitlement_version="1",
        entitlement_hash=calculate_entitlement_hash(**fields),
    )


def fulfillment_execution_for_test(
    execution_state: FulfillmentExecutionState,
) -> FulfillmentExecution:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    succeeded = execution_state is FulfillmentExecutionState.SUCCEEDED
    failed = execution_state in {
        FulfillmentExecutionState.PERMANENT_FAILURE,
        FulfillmentExecutionState.RECONCILIATION_REQUIRED,
    }
    return FulfillmentExecution(
        id="ful_00000000000000000000000000",
        entitlement_id="ent_00000000000000000000000000",
        account_id="acct_00000000000000000000000000",
        transaction_id="txn_00000000000000000000000000",
        merchant_id="mrc_00000000000000000000000000",
        service_id="svc_00000000000000000000000000",
        input_hash=HASH,
        execution_state=execution_state,
        attempt_count=1,
        started_at=now,
        completed_at=now + timedelta(seconds=1) if succeeded else None,
        failed_at=now + timedelta(seconds=1) if failed else None,
        result_content_type="application/json" if succeeded else None,
        result_json={"ok": True} if succeeded else None,
        result_hash=HASH if succeeded else None,
        result_size_bytes=11 if succeeded else None,
        failure_code="FULFILLMENT_FAILED" if failed else None,
        compensation_required=(execution_state is FulfillmentExecutionState.PERMANENT_FAILURE),
        revision=1,
        lease_generation=1,
        lease_expires_at=None,
    )


def test_entitlement_has_no_updated_at_and_rejects_orm_mutation() -> None:
    assert "updated_at" not in Entitlement.__table__.columns
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        entitlement = entitlement_for_test()
        session.add(entitlement)
        session.commit()

        entitlement.currency = "USD"
        with pytest.raises(InvalidRequestError, match="immutable"):
            session.commit()

    engine.dispose()


def test_entitlement_insert_rejects_hash_mismatch() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        entitlement = entitlement_for_test()
        entitlement.provider_payment_id = "pay_tampered"
        session.add(entitlement)
        with pytest.raises(InvalidRequestError, match="hash"):
            session.commit()

    engine.dispose()


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("result_content_type", "application/problem+json"),
        ("result_json", {"ok": False}),
        ("result_hash", "sha256:" + "2" * 64),
        ("result_size_bytes", 12),
        ("completed_at", datetime(2026, 8, 26, 12, 0, 2, tzinfo=UTC)),
    ),
)
def test_succeeded_fulfillment_result_evidence_is_immutable_at_orm_boundary(
    field: str,
    replacement: object,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        execution = fulfillment_execution_for_test(FulfillmentExecutionState.SUCCEEDED)
        session.add(execution)
        session.commit()
        session.refresh(execution)

        setattr(execution, field, replacement)
        execution.revision = 2
        with pytest.raises(InvalidRequestError, match="result evidence is immutable"):
            session.commit()

    engine.dispose()


@pytest.mark.parametrize(
    ("execution_state", "field", "replacement"),
    (
        (
            FulfillmentExecutionState.SUCCEEDED,
            "execution_state",
            FulfillmentExecutionState.EXECUTING,
        ),
        (
            FulfillmentExecutionState.PERMANENT_FAILURE,
            "failure_code",
            "FULFILLMENT_FAILURE_REWRITTEN",
        ),
    ),
)
def test_fulfillment_terminal_state_and_evidence_are_monotonic_at_orm_boundary(
    execution_state: FulfillmentExecutionState,
    field: str,
    replacement: object,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        execution = fulfillment_execution_for_test(execution_state)
        session.add(execution)
        session.commit()
        session.refresh(execution)

        setattr(execution, field, replacement)
        execution.revision = 2
        with pytest.raises(InvalidRequestError, match="Terminal fulfillment.*immutable"):
            session.commit()

    engine.dispose()


def test_fulfillment_route_snapshot_can_be_set_once_then_is_immutable() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        execution = fulfillment_execution_for_test(FulfillmentExecutionState.RETRYABLE_FAILURE)
        execution.failed_at = datetime(2026, 8, 26, 12, 0, 1, tzinfo=UTC)
        execution.failure_code = "CELESTRAK_UNAVAILABLE"
        session.add(execution)
        session.commit()
        session.refresh(execution)

        execution.fulfillment_config_id = "sfc_00000000000000000000000000"
        execution.fulfillment_config_revision = 1
        execution.provider_type = FulfillmentProviderType.HTTP
        execution.endpoint_url = "http://127.0.0.1:8100/internal/v1/fulfillments"
        execution.request_timeout_seconds = 15
        execution.maximum_attempts = 3
        execution.revision = 2
        session.commit()
        session.refresh(execution)

        execution.endpoint_url = "http://127.0.0.1:8100/internal/v1/alternate"
        execution.revision = 3
        with pytest.raises(InvalidRequestError, match="route snapshot is immutable"):
            session.commit()

    engine.dispose()


def test_reconciliation_evidence_requires_an_explicit_recovery_transition() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        execution = fulfillment_execution_for_test(
            FulfillmentExecutionState.RECONCILIATION_REQUIRED
        )
        session.add(execution)
        session.commit()
        session.refresh(execution)

        execution.failure_code = "FULFILLMENT_FAILURE_REWRITTEN"
        execution.revision = 2
        with pytest.raises(InvalidRequestError, match="explicit recovery transition"):
            session.commit()

        session.rollback()
        execution.execution_state = FulfillmentExecutionState.EXECUTING
        execution.failed_at = None
        execution.failure_code = None
        execution.lease_generation += 1
        execution.lease_expires_at = datetime(2026, 8, 26, 12, 5, tzinfo=UTC)
        execution.revision += 1
        session.commit()

        assert execution.execution_state is FulfillmentExecutionState.EXECUTING
        assert execution.revision == 2

    engine.dispose()


def test_payment_reverification_event_is_transaction_scoped_and_immutable() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as session:
        event = FulfillmentEvent(
            id="fve_00000000000000000000000000",
            transaction_id="txn_00000000000000000000000000",
            entitlement_id=None,
            execution_id=None,
            execution_revision=None,
            event_type=FulfillmentEventType.PAYMENT_REVERIFIED,
            actor_type=FulfillmentEventActorType.ENTITLEMENT_WORKER,
            actor_id=None,
            reason_code="PAYMENT_REVERIFIED",
            event_metadata={"provider": "razorpay"},
            idempotency_key="payment-reverified:test",
            occurred_at=now,
        )
        session.add(event)
        session.commit()

        event.reason_code = "CHANGED"
        with pytest.raises(InvalidRequestError, match="immutable"):
            session.commit()

    engine.dispose()
