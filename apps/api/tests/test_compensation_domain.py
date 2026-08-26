from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.orm import Session

from app.domain.compensation_hashing import calculate_failure_evidence_hash
from app.domain.compensation_policy import (
    CompensationPolicyReasonCode,
    evaluate_compensation_policy,
)
from app.domain.enums import (
    CompensationDecisionProvenance,
    CompensationDecisionState,
    CompensationEventActorType,
    CompensationEventType,
    CompensationRecommendedAction,
    FulfillmentExecutionState,
    PaymentAttemptStatus,
    PaymentProvider,
    PaymentRefundState,
    PurchaseType,
    RefundOutboxEventType,
)
from app.domain.hashing import calculate_quote_hash, sha256_json
from app.models import (
    Base,
    CompensationCase,
    CompensationEvent,
    PaymentRefund,
    RefundOutboxEvent,
)

HASH = "sha256:" + "1" * 64
NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)


def _quote(*, refund_on_failure: bool = True) -> SimpleNamespace:
    input_value = {"norad_id": 25544}
    input_hash = sha256_json(input_value)
    snapshot = {
        "version": "1",
        "json_schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        "merchant": {"id": "mrc_00000000000000000000000000", "slug": "orbit", "name": "Orbit"},
        "service": {
            "id": "svc_00000000000000000000000000",
            "slug": "track",
            "name": "Track",
            "service_type": "digital_asset",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "output_content_type": "application/json",
        },
        "pricing": {"amount": 500, "currency": "INR", "purchase_type": "one_time"},
        "fulfillment": {"maximum_seconds": 60, "refund_on_failure": refund_on_failure},
    }
    fields = {
        "quote_id": "qte_00000000000000000000000000",
        "merchant_id": "mrc_00000000000000000000000000",
        "service_id": "svc_00000000000000000000000000",
        "service_snapshot": snapshot,
        "input_value": input_value,
        "input_hash": input_hash,
        "amount": 500,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "maximum_fulfillment_seconds": 60,
        "refund_on_fulfillment_failure": refund_on_failure,
        "issued_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(minutes=5),
    }
    return SimpleNamespace(
        id=fields["quote_id"],
        merchant_id=fields["merchant_id"],
        service_id=fields["service_id"],
        service_snapshot=snapshot,
        input=input_value,
        input_hash=input_hash,
        amount=500,
        currency="INR",
        purchase_type=PurchaseType.ONE_TIME,
        maximum_fulfillment_seconds=60,
        refund_on_fulfillment_failure=refund_on_failure,
        issued_at=fields["issued_at"],
        expires_at=fields["expires_at"],
        quote_hash=calculate_quote_hash(**fields),
    )


def _policy_evidence(*, refund_on_failure: bool = True) -> tuple[SimpleNamespace, ...]:
    quote = _quote(refund_on_failure=refund_on_failure)
    transaction = SimpleNamespace(
        id="txn_00000000000000000000000000",
        account_id="acct_00000000000000000000000000",
        quote_id=quote.id,
        quote_hash=quote.quote_hash,
        merchant_id=quote.merchant_id,
        service_id=quote.service_id,
        amount=500,
        currency="INR",
        transaction_state="paid",
    )
    attempt = SimpleNamespace(
        id="pmt_00000000000000000000000000",
        transaction_id=transaction.id,
        provider_payment_id="pay_exact",
        amount=500,
        currency="INR",
        provider_status=PaymentAttemptStatus.CAPTURED,
        captured=True,
    )
    entitlement = SimpleNamespace(
        id="ent_00000000000000000000000000",
        account_id=transaction.account_id,
        transaction_id=transaction.id,
        provider_payment_id=attempt.provider_payment_id,
        quote_id=quote.id,
        quote_hash=quote.quote_hash,
        merchant_id=quote.merchant_id,
        service_id=quote.service_id,
        input_hash=quote.input_hash,
        amount=500,
        currency="INR",
    )
    fulfillment = SimpleNamespace(
        id="ful_00000000000000000000000000",
        entitlement_id=entitlement.id,
        account_id=transaction.account_id,
        transaction_id=transaction.id,
        merchant_id=quote.merchant_id,
        service_id=quote.service_id,
        input_hash=quote.input_hash,
        execution_state=FulfillmentExecutionState.PERMANENT_FAILURE,
        compensation_required=True,
        result_content_type=None,
        result_json=None,
        result_hash=None,
        result_size_bytes=None,
    )
    return transaction, attempt, entitlement, fulfillment, quote


def _case(case_id: str = "cmp_00000000000000000000000000") -> CompensationCase:
    return CompensationCase(
        id=case_id,
        account_id="acct_00000000000000000000000000",
        transaction_id="txn_00000000000000000000000000",
        payment_attempt_id="pmt_00000000000000000000000000",
        entitlement_id="ent_00000000000000000000000000",
        fulfillment_execution_id="ful_00000000000000000000000000",
        quote_id="qte_00000000000000000000000000",
        quote_hash=HASH,
        merchant_id="mrc_00000000000000000000000000",
        service_id="svc_00000000000000000000000000",
        amount_paid=500,
        currency="INR",
        failure_code="FULFILLMENT_FAILED",
        failure_evidence_version="1",
        failure_evidence_hash=HASH,
        recommended_action=CompensationRecommendedAction.FULL_REFUND,
        decision_state=CompensationDecisionState.APPROVED,
        decision_provenance=CompensationDecisionProvenance.AUTOMATIC_APPROVED,
        approved_refund_amount=500,
        decision_reason_code="COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED",
        created_at=NOW,
        decided_at=NOW,
        closed_at=None,
        revision=1,
    )


def _refund(refund_id: str) -> PaymentRefund:
    return PaymentRefund(
        id=refund_id,
        compensation_case_id="cmp_00000000000000000000000000",
        transaction_id="txn_00000000000000000000000000",
        payment_attempt_id="pmt_00000000000000000000000000",
        provider=PaymentProvider.RAZORPAY,
        provider_payment_id="pay_exact",
        provider_refund_id=None,
        amount=200,
        currency="INR",
        refund_state=PaymentRefundState.REFUND_PENDING,
        provider_status=None,
        provider_receipt=refund_id,
        created_at=NOW,
        revision=1,
    )


def test_failure_evidence_hash_is_canonical_across_timezone_offsets() -> None:
    fields = {
        "fulfillment_execution_id": "ful_00000000000000000000000000",
        "transaction_id": "txn_00000000000000000000000000",
        "entitlement_id": "ent_00000000000000000000000000",
        "failure_code": "FULFILLMENT_FAILED",
        "attempt_count": 3,
        "service_id": "svc_00000000000000000000000000",
        "input_hash": HASH,
        "result_absent": True,
        "started_at": NOW,
        "failed_at": NOW + timedelta(seconds=1),
    }
    utc_hash = calculate_failure_evidence_hash(**fields)
    fields["started_at"] = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    fields["failed_at"] = (NOW + timedelta(seconds=1)).astimezone(
        timezone(timedelta(hours=5, minutes=30))
    )
    assert calculate_failure_evidence_hash(**fields) == utc_hash


def test_compensation_policy_automatically_approves_only_quote_authorized_full_refund() -> None:
    transaction, attempt, entitlement, fulfillment, quote = _policy_evidence()
    result = evaluate_compensation_policy(
        transaction=transaction,
        payment_attempt=attempt,
        entitlement=entitlement,
        fulfillment=fulfillment,
        quote=quote,
    )
    assert result.create_case is True
    assert result.decision_state is CompensationDecisionState.APPROVED
    assert result.decision_provenance is CompensationDecisionProvenance.AUTOMATIC_APPROVED
    assert result.recommended_action is CompensationRecommendedAction.FULL_REFUND
    assert result.approved_refund_amount == 500


def test_compensation_policy_routes_quote_opt_out_to_manual_review() -> None:
    transaction, attempt, entitlement, fulfillment, quote = _policy_evidence(
        refund_on_failure=False
    )
    result = evaluate_compensation_policy(
        transaction=transaction,
        payment_attempt=attempt,
        entitlement=entitlement,
        fulfillment=fulfillment,
        quote=quote,
    )
    assert result.create_case is True
    assert result.decision_state is CompensationDecisionState.MANUAL_REVIEW
    assert result.approved_refund_amount is None
    assert result.reason_code is CompensationPolicyReasonCode.COMPENSATION_MANUAL_REVIEW_REQUIRED


def test_compensation_policy_does_not_create_case_when_value_exists() -> None:
    transaction, attempt, entitlement, fulfillment, quote = _policy_evidence()
    fulfillment.result_hash = HASH
    result = evaluate_compensation_policy(
        transaction=transaction,
        payment_attempt=attempt,
        entitlement=entitlement,
        fulfillment=fulfillment,
        quote=quote,
    )
    assert result.create_case is False
    assert result.reason_code is (
        CompensationPolicyReasonCode.COMPENSATION_NOT_ELIGIBLE_VALUE_DELIVERED
    )


@pytest.mark.parametrize(
    ("scenario", "expected_reason"),
    [
        (
            "unpaid_transaction",
            CompensationPolicyReasonCode.COMPENSATION_NOT_ELIGIBLE_PAYMENT_NOT_PAID,
        ),
        (
            "failed_payment_attempt",
            CompensationPolicyReasonCode.COMPENSATION_NOT_ELIGIBLE_CAPTURE_MISSING,
        ),
        (
            "retryable_fulfillment",
            CompensationPolicyReasonCode.COMPENSATION_NOT_REQUIRED,
        ),
        (
            "successful_fulfillment",
            CompensationPolicyReasonCode.COMPENSATION_NOT_REQUIRED,
        ),
    ],
)
def test_compensation_policy_rejects_noneligible_payment_and_fulfillment_states(
    scenario: str,
    expected_reason: CompensationPolicyReasonCode,
) -> None:
    transaction, attempt, entitlement, fulfillment, quote = _policy_evidence()
    if scenario == "unpaid_transaction":
        transaction.transaction_state = "payment_pending"
    elif scenario == "failed_payment_attempt":
        attempt.provider_status = PaymentAttemptStatus.FAILED
        attempt.captured = False
    elif scenario == "retryable_fulfillment":
        fulfillment.execution_state = FulfillmentExecutionState.RETRYABLE_FAILURE
        fulfillment.compensation_required = False
    else:
        fulfillment.execution_state = FulfillmentExecutionState.SUCCEEDED
        fulfillment.compensation_required = False

    result = evaluate_compensation_policy(
        transaction=transaction,
        payment_attempt=attempt,
        entitlement=entitlement,
        fulfillment=fulfillment,
        quote=quote,
    )

    assert result.create_case is False
    assert result.approved_refund_amount is None
    assert result.reason_code is expected_reason


def test_case_preserves_approved_decision_when_execution_enters_manual_review() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        case = _case()
        case.decision_state = CompensationDecisionState.EXECUTING
        session.add(case)
        session.commit()
        case.decision_state = CompensationDecisionState.MANUAL_REVIEW
        case.revision = 2
        session.commit()
        assert case.decision_provenance is CompensationDecisionProvenance.AUTOMATIC_APPROVED
        assert case.approved_refund_amount == 500
        assert case.decision_reason_code == "COMPENSATION_FULL_REFUND_FULFILLMENT_FAILED"
    engine.dispose()


def test_compensation_case_evidence_is_immutable_at_orm_boundary() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        case = _case()
        session.add(case)
        session.commit()
        case.failure_code = "CHANGED"
        case.revision = 2
        with pytest.raises(InvalidRequestError, match="evidence and bindings are immutable"):
            session.commit()
    engine.dispose()


def test_only_one_active_refund_is_allowed_per_case() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_case())
        session.add(_refund("rfd_00000000000000000000000000"))
        session.commit()
        session.add(_refund("rfd_00000000000000000000000001"))
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()


@pytest.mark.parametrize(
    ("state", "provider_refund_id", "provider_status", "terminal_field"),
    [
        (PaymentRefundState.REFUNDED, "rfnd_exact", "pending", "processed_at"),
        (PaymentRefundState.REFUND_FAILED, "rfnd_exact", "processed", "failed_at"),
    ],
)
def test_terminal_refund_requires_coherent_provider_evidence(
    state: PaymentRefundState,
    provider_refund_id: str,
    provider_status: str,
    terminal_field: str,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        refund = _refund("rfd_00000000000000000000000002")
        refund.refund_state = state
        refund.provider_refund_id = provider_refund_id
        refund.provider_status = provider_status
        refund.requested_at = NOW
        setattr(refund, terminal_field, NOW)
        session.add_all((_case(), refund))
        with pytest.raises(IntegrityError, match="terminal_provider_evidence"):
            session.commit()
    engine.dispose()


def test_failed_refund_permits_only_rejection_or_exact_provider_failure_shapes() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        rejected = _refund("rfd_00000000000000000000000003")
        rejected.refund_state = PaymentRefundState.REFUND_FAILED
        rejected.requested_at = NOW
        rejected.failed_at = NOW

        provider_failed = _refund("rfd_00000000000000000000000004")
        provider_failed.refund_state = PaymentRefundState.REFUND_FAILED
        provider_failed.provider_refund_id = "rfnd_exact"
        provider_failed.provider_status = "failed"
        provider_failed.requested_at = NOW
        provider_failed.failed_at = NOW

        session.add_all((_case(), rejected, provider_failed))
        session.commit()
    engine.dispose()


def test_terminal_refund_allows_only_revision_and_reconciliation_overlay_updates() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        refund = _refund("rfd_00000000000000000000000005")
        refund.refund_state = PaymentRefundState.REFUNDED
        refund.provider_refund_id = "rfnd_exact"
        refund.provider_status = "processed"
        refund.requested_at = NOW
        refund.processed_at = NOW
        session.add_all((_case(), refund))
        session.commit()

        next_revision = refund.revision + 1
        refund.reconciliation_required_at = NOW + timedelta(seconds=1)
        refund.reconciliation_reason_code = "REFUND_TERMINAL_EVIDENCE_CONFLICT"
        refund.revision = next_revision
        session.commit()

        next_revision = refund.revision + 1
        refund.reconciliation_required_at = NOW
        refund.revision = next_revision
        with pytest.raises(InvalidRequestError, match="overlay time cannot regress"):
            session.commit()
        session.rollback()

        next_revision = refund.revision + 1
        refund.provider_status = "failed"
        refund.revision = next_revision
        with pytest.raises(InvalidRequestError, match="Terminal payment refund evidence"):
            session.commit()
        session.rollback()

        next_revision = refund.revision + 1
        refund.reconciliation_required_at = None
        refund.reconciliation_reason_code = None
        refund.last_reconciled_at = NOW + timedelta(seconds=2)
        refund.revision = next_revision
        session.commit()
        assert refund.last_reconciled_at is not None
    engine.dispose()


def test_reconciliation_overlay_requires_a_paired_stable_reason_code() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        refund = _refund("rfd_00000000000000000000000006")
        refund.requested_at = NOW
        refund.refund_state = PaymentRefundState.REFUND_PROCESSING
        refund.reconciliation_required_at = NOW + timedelta(seconds=1)
        refund.reconciliation_reason_code = "not-stable"
        session.add_all((_case(), refund))
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()


def test_approved_decision_provenance_and_reason_cannot_change_during_execution() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        case = _case()
        session.add(case)
        session.commit()
        case.decision_state = CompensationDecisionState.EXECUTING
        case.decision_provenance = CompensationDecisionProvenance.MANUAL_APPROVED
        case.decision_reason_code = "CHANGED_AFTER_APPROVAL"
        case.revision = 2
        with pytest.raises(InvalidRequestError, match="provenance and reason are immutable"):
            session.commit()
    engine.dispose()


def test_compensation_event_and_refund_outbox_payload_are_immutable() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        case = _case()
        event = CompensationEvent(
            id="cpe_00000000000000000000000000",
            compensation_case_id=case.id,
            transaction_id=case.transaction_id,
            payment_refund_id=None,
            sequence=1,
            case_revision=1,
            refund_revision=None,
            event_type=CompensationEventType.COMPENSATION_CASE_CREATED,
            actor_type=CompensationEventActorType.SYSTEM,
            actor_id=None,
            reason_code="COMPENSATION_CASE_CREATED",
            event_metadata={"failure_evidence_version": "1"},
            idempotency_key=f"compensation:{case.id}:created",
            occurred_at=NOW,
        )
        outbox = RefundOutboxEvent(
            id="rox_00000000000000000000000000",
            event_type=RefundOutboxEventType.REFUND_REQUESTED,
            compensation_case_id=case.id,
            deduplication_key=f"refund:{case.id}",
            payload_version="1",
            payload={"compensation_case_id": case.id},
            created_at=NOW,
            available_at=NOW,
        )
        session.add_all((case, event, outbox))
        session.commit()
        event.reason_code = "CHANGED"
        with pytest.raises(InvalidRequestError, match="immutable"):
            session.commit()
        session.rollback()
        outbox.payload = {"changed": True}
        with pytest.raises(InvalidRequestError, match="immutable"):
            session.commit()
    engine.dispose()
