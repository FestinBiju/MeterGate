from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.enums import PolicyDecision, PurchaseType, ServiceType
from app.domain.exceptions import (
    PolicyEvaluationIntegrityError,
    PolicyIntegrityError,
    PolicyTTLExceededError,
    QuoteIntegrityError,
    ResourceNotFoundError,
)
from app.domain.hashing import calculate_quote_hash, sha256_json
from app.domain.integrity import IntegrityStructureError
from app.domain.policy_engine import (
    PolicyReasonCode,
    PolicyRule,
    evaluate_policy,
)
from app.domain.policy_hashing import (
    POLICY_VERSION,
    calculate_policy_hash,
    normalize_allowlist,
    recompute_policy_hash,
    verify_policy_integrity,
)
from app.models import BuyerPolicy, PolicyEvaluation, Quote
from app.schemas.policies import BuyerPolicyCreate
from app.schemas.policy_evaluations import PolicyEvaluationCreate
from app.services.policies import BuyerPolicyApplicationService
from app.services.policy_evaluations import PolicyEvaluationApplicationService

POLICY_ID = "pol_00000000000000000000000001"
OTHER_POLICY_ID = "pol_00000000000000000000000002"
EVALUATION_ID = "pye_00000000000000000000000001"
QUOTE_ID = "qte_00000000000000000000000001"
MERCHANT_ID = "mrc_00000000000000000000000001"
OTHER_MERCHANT_ID = "mrc_00000000000000000000000002"
SERVICE_ID = "svc_00000000000000000000000001"
OTHER_SERVICE_ID = "svc_00000000000000000000000002"
ISSUED_AT = datetime(2026, 8, 25, 12, tzinfo=UTC)


@dataclass
class FrozenClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


class FakePolicyRepository:
    def __init__(self, policies: list[BuyerPolicy] | None = None) -> None:
        self.records = {policy.id: policy for policy in policies or []}

    async def create(self, policy: BuyerPolicy) -> BuyerPolicy:
        policy.created_at = policy.issued_at
        self.records[policy.id] = policy
        return policy

    async def get(self, policy_id: str) -> BuyerPolicy | None:
        return self.records.get(policy_id)


class FakeQuoteRepository:
    def __init__(self, quotes: list[Quote] | None = None) -> None:
        self.records = {quote.id: quote for quote in quotes or []}

    async def get(self, quote_id: str) -> Quote | None:
        return self.records.get(quote_id)


class FakeEvaluationRepository:
    def __init__(self, evaluations: list[PolicyEvaluation] | None = None) -> None:
        self.records = {evaluation.id: evaluation for evaluation in evaluations or []}

    async def create(self, evaluation: PolicyEvaluation) -> PolicyEvaluation:
        evaluation.created_at = evaluation.evaluated_at
        self.records[evaluation.id] = evaluation
        return evaluation

    async def get(self, evaluation_id: str) -> PolicyEvaluation | None:
        return self.records.get(evaluation_id)


def policy_hash_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "policy_id": POLICY_ID,
        "subject_ref": "dev-user-001",
        "maximum_amount": 1000,
        "allowed_currencies": ["INR"],
        "allowed_merchant_ids": [MERCHANT_ID],
        "allowed_service_ids": [SERVICE_ID],
        "allowed_service_types": ["report"],
        "allowed_purchase_types": ["one_time"],
        "issued_at": ISSUED_AT,
        "expires_at": ISSUED_AT + timedelta(minutes=15),
        "policy_version": POLICY_VERSION,
    }
    fields.update(overrides)
    return fields


def make_policy(**overrides: Any) -> BuyerPolicy:
    fields = policy_hash_fields(**overrides)
    return BuyerPolicy(
        id=fields["policy_id"],
        subject_ref=fields["subject_ref"],
        maximum_amount=fields["maximum_amount"],
        allowed_currencies=fields["allowed_currencies"],
        allowed_merchant_ids=fields["allowed_merchant_ids"],
        allowed_service_ids=fields["allowed_service_ids"],
        allowed_service_types=fields["allowed_service_types"],
        allowed_purchase_types=fields["allowed_purchase_types"],
        issued_at=fields["issued_at"],
        expires_at=fields["expires_at"],
        policy_version=fields["policy_version"],
        policy_hash=calculate_policy_hash(**fields),
    )


def make_quote(
    *,
    amount: int = 500,
    currency: str = "INR",
    service_type: str = "report",
    purchase_type: str = "one_time",
    issued_at: datetime = ISSUED_AT,
    expires_at: datetime | None = None,
) -> Quote:
    input_value = {"norad_id": 25544}
    input_hash = sha256_json(input_value)
    snapshot = {
        "version": "1",
        "json_schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        "merchant": {"id": MERCHANT_ID, "slug": "orbitintel", "name": "OrbitIntel"},
        "service": {
            "id": SERVICE_ID,
            "slug": "orbital-risk-report",
            "name": "Orbital Risk Report",
            "service_type": service_type,
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "output_content_type": "application/json",
        },
        "pricing": {
            "amount": amount,
            "currency": currency,
            "purchase_type": purchase_type,
        },
        "fulfillment": {"maximum_seconds": 30, "refund_on_failure": True},
    }
    effective_expiry = expires_at or issued_at + timedelta(minutes=5)
    hash_fields = {
        "quote_id": QUOTE_ID,
        "merchant_id": MERCHANT_ID,
        "service_id": SERVICE_ID,
        "service_snapshot": snapshot,
        "input_value": input_value,
        "input_hash": input_hash,
        "amount": amount,
        "currency": currency,
        "purchase_type": purchase_type,
        "maximum_fulfillment_seconds": 30,
        "refund_on_fulfillment_failure": True,
        "issued_at": issued_at,
        "expires_at": effective_expiry,
    }
    return Quote(
        id=QUOTE_ID,
        merchant_id=MERCHANT_ID,
        service_id=SERVICE_ID,
        service_snapshot=snapshot,
        input=input_value,
        input_hash=input_hash,
        amount=amount,
        currency=currency,
        purchase_type=PurchaseType(purchase_type),
        maximum_fulfillment_seconds=30,
        refund_on_fulfillment_failure=True,
        issued_at=issued_at,
        expires_at=effective_expiry,
        quote_hash=calculate_quote_hash(**hash_fields),
    )


def policy_payload(**overrides: Any) -> BuyerPolicyCreate:
    values: dict[str, Any] = {
        "subject_ref": "dev-user-001",
        "maximum_amount": 1000,
        "allowed_currencies": ["INR"],
        "allowed_merchant_ids": [MERCHANT_ID],
        "allowed_service_ids": [SERVICE_ID],
        "allowed_service_types": ["report"],
        "allowed_purchase_types": ["one_time"],
        "expires_in_seconds": 900,
    }
    values.update(overrides)
    return BuyerPolicyCreate.model_validate(values)


def test_policy_schema_normalizes_sets_and_omission_means_unconstrained() -> None:
    payload = BuyerPolicyCreate(
        subject_ref=" dev-user-001 ",
        maximum_amount=1000,
        allowed_currencies=["USD", "INR"],
        allowed_service_ids=[OTHER_SERVICE_ID, SERVICE_ID],
        expires_in_seconds=900,
    )

    assert payload.subject_ref == "dev-user-001"
    assert payload.allowed_currencies == ["INR", "USD"]
    assert payload.allowed_service_ids == [SERVICE_ID, OTHER_SERVICE_ID]
    assert payload.allowed_merchant_ids is None
    assert payload.allowed_service_types is None
    assert payload.allowed_purchase_types is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"maximum_amount": -1},
        {"maximum_amount": True},
        {"maximum_amount": 1.5},
        {"allowed_currencies": []},
        {"allowed_currencies": ["INR", "INR"]},
        {"allowed_currencies": ["inr"]},
        {"allowed_service_types": ["unknown"]},
        {"expires_in_seconds": True},
    ],
)
def test_policy_schema_rejects_ambiguous_or_malformed_constraints(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        policy_payload(**overrides)


def test_policy_schema_bounds_allowlist_size() -> None:
    currencies = [
        f"{chr(65 + first)}{chr(65 + second)}{chr(65 + third)}"
        for first in range(5)
        for second in range(5)
        for third in range(5)
    ]

    with pytest.raises(ValidationError):
        policy_payload(allowed_currencies=currencies[:101])
    with pytest.raises(ValueError, match="bounded"):
        normalize_allowlist(currencies[:101])
    with pytest.raises(ValueError, match="list of strings"):
        normalize_allowlist({"INR": "ignored"})  # type: ignore[arg-type]


def test_policy_hash_normalizes_allowlist_order_and_binds_every_term() -> None:
    first = policy_hash_fields(
        allowed_currencies=["USD", "INR"],
        allowed_service_ids=[OTHER_SERVICE_ID, SERVICE_ID],
    )
    reordered = policy_hash_fields(
        allowed_currencies=["INR", "USD"],
        allowed_service_ids=[SERVICE_ID, OTHER_SERVICE_ID],
    )

    assert calculate_policy_hash(**first) == calculate_policy_hash(**reordered)
    for field, replacement in (
        ("maximum_amount", 1001),
        ("allowed_merchant_ids", [OTHER_MERCHANT_ID]),
        ("allowed_service_ids", [OTHER_SERVICE_ID]),
        ("expires_at", ISSUED_AT + timedelta(minutes=16)),
    ):
        assert calculate_policy_hash(**first) != calculate_policy_hash(
            **{**first, field: replacement}
        )


def test_policy_hash_is_recomputable() -> None:
    policy = make_policy()

    assert recompute_policy_hash(policy) == policy.policy_hash
    assert verify_policy_integrity(policy).hash_matches is True


def test_pure_engine_allows_in_fixed_complete_order() -> None:
    result = evaluate_policy(make_policy(), make_quote(), ISSUED_AT + timedelta(minutes=1))

    assert result.decision is PolicyDecision.ALLOW
    assert result.reason_codes == (PolicyReasonCode.ALLOW_POLICY_SATISFIED,)
    assert [check.rule for check in result.checks] == list(PolicyRule)
    assert all(check.result.value == "pass" for check in result.checks)


def test_pure_engine_reports_all_safe_independent_failures_in_order() -> None:
    policy = make_policy(
        maximum_amount=100,
        allowed_currencies=["USD"],
        allowed_merchant_ids=[OTHER_MERCHANT_ID],
        allowed_service_ids=[OTHER_SERVICE_ID],
        allowed_service_types=[ServiceType.API.value],
        allowed_purchase_types=[PurchaseType.SUBSCRIPTION.value],
    )

    result = evaluate_policy(policy, make_quote(), ISSUED_AT + timedelta(minutes=1))

    assert result.decision is PolicyDecision.DENY
    assert result.reason_codes == (
        PolicyReasonCode.DENY_AMOUNT_EXCEEDS_LIMIT,
        PolicyReasonCode.DENY_CURRENCY_NOT_ALLOWED,
        PolicyReasonCode.DENY_MERCHANT_NOT_ALLOWED,
        PolicyReasonCode.DENY_SERVICE_NOT_ALLOWED,
        PolicyReasonCode.DENY_SERVICE_TYPE_NOT_ALLOWED,
        PolicyReasonCode.DENY_PURCHASE_TYPE_NOT_ALLOWED,
    )


@pytest.mark.parametrize(
    ("policy_overrides", "expected_reason"),
    [
        ({"maximum_amount": 499}, PolicyReasonCode.DENY_AMOUNT_EXCEEDS_LIMIT),
        ({"allowed_currencies": ["USD"]}, PolicyReasonCode.DENY_CURRENCY_NOT_ALLOWED),
        (
            {"allowed_merchant_ids": [OTHER_MERCHANT_ID]},
            PolicyReasonCode.DENY_MERCHANT_NOT_ALLOWED,
        ),
        (
            {"allowed_service_ids": [OTHER_SERVICE_ID]},
            PolicyReasonCode.DENY_SERVICE_NOT_ALLOWED,
        ),
        (
            {"allowed_service_types": [ServiceType.API.value]},
            PolicyReasonCode.DENY_SERVICE_TYPE_NOT_ALLOWED,
        ),
        (
            {"allowed_purchase_types": [PurchaseType.SUBSCRIPTION.value]},
            PolicyReasonCode.DENY_PURCHASE_TYPE_NOT_ALLOWED,
        ),
    ],
)
def test_pure_engine_isolates_each_commercial_denial(
    policy_overrides: dict[str, Any],
    expected_reason: PolicyReasonCode,
) -> None:
    result = evaluate_policy(
        make_policy(**policy_overrides),
        make_quote(),
        ISSUED_AT + timedelta(minutes=1),
    )

    assert result.decision is PolicyDecision.DENY
    assert result.reason_codes == (expected_reason,)


def test_pure_engine_expiry_boundaries_are_inclusive_and_allowlists_can_be_null() -> None:
    policy = make_policy(
        allowed_currencies=None,
        allowed_merchant_ids=None,
        allowed_service_ids=None,
        allowed_service_types=None,
        allowed_purchase_types=None,
    )
    quote = make_quote()

    policy_expired = evaluate_policy(policy, quote, policy.expires_at)
    quote_expired = evaluate_policy(policy, quote, quote.expires_at)

    assert PolicyReasonCode.DENY_POLICY_EXPIRED in policy_expired.reason_codes
    assert PolicyReasonCode.DENY_QUOTE_EXPIRED in quote_expired.reason_codes
    currency_check = next(
        check for check in quote_expired.checks if check.rule is PolicyRule.CURRENCY
    )
    assert currency_check.details["allowed"] is None


def test_pure_engine_accepts_exact_issuance_and_rejects_future_issued_sources() -> None:
    policy = make_policy()
    quote = make_quote()

    exact_issue = evaluate_policy(policy, quote, ISSUED_AT)

    assert exact_issue.decision is PolicyDecision.ALLOW
    with pytest.raises(IntegrityStructureError, match="policy"):
        evaluate_policy(policy, quote, ISSUED_AT - timedelta(microseconds=1))

    future_quote = make_quote(
        issued_at=ISSUED_AT + timedelta(seconds=1),
        expires_at=ISSUED_AT + timedelta(minutes=5),
    )
    with pytest.raises(IntegrityStructureError, match="quote"):
        evaluate_policy(policy, future_quote, ISSUED_AT)


def test_integrity_mismatch_short_circuits_ordinary_policy_checks() -> None:
    policy = make_policy()
    quote = make_quote()
    policy.maximum_amount = 999
    quote.amount = 499

    result = evaluate_policy(policy, quote, ISSUED_AT + timedelta(minutes=1))

    assert result.decision is PolicyDecision.DENY
    assert result.reason_codes == (
        PolicyReasonCode.INTEGRITY_POLICY_HASH_MISMATCH,
        PolicyReasonCode.INTEGRITY_QUOTE_HASH_MISMATCH,
    )
    assert [check.rule for check in result.checks] == [
        PolicyRule.POLICY_INTEGRITY,
        PolicyRule.QUOTE_INTEGRITY,
    ]


@pytest.mark.asyncio
async def test_policy_application_creates_normalized_fresh_policy_and_derives_expiry() -> None:
    repository = FakePolicyRepository()
    clock = FrozenClock(ISSUED_AT)
    application = BuyerPolicyApplicationService(  # type: ignore[arg-type]
        repository,
        maximum_ttl=timedelta(hours=1),
        clock=clock,
    )

    response = await application.create(
        policy_payload(
            allowed_currencies=["USD", "INR"],
            allowed_service_ids=[OTHER_SERVICE_ID, SERVICE_ID],
        )
    )

    assert response.id.startswith("pol_")
    assert response.constraints.allowed_currencies == ["INR", "USD"]
    assert response.constraints.allowed_service_ids == [SERVICE_ID, OTHER_SERVICE_ID]
    assert response.expires_at - response.issued_at == timedelta(minutes=15)
    assert response.created_at == response.issued_at
    persisted = repository.records[response.id]
    assert recompute_policy_hash(persisted) == persisted.policy_hash

    clock.current = response.issued_at - timedelta(microseconds=1)
    with pytest.raises(PolicyIntegrityError) as future_error:
        await application.get(response.id)
    assert future_error.value.reason_code == "INTEGRITY_POLICY_DATA_INVALID"

    clock.current = response.issued_at
    assert (await application.get(response.id)).state == "active"
    clock.current = response.expires_at
    assert (await application.get(response.id)).state == "expired"


@pytest.mark.asyncio
async def test_policy_application_enforces_configured_ttl_and_integrity() -> None:
    repository = FakePolicyRepository()
    application = BuyerPolicyApplicationService(  # type: ignore[arg-type]
        repository,
        maximum_ttl=timedelta(minutes=5),
        clock=FrozenClock(ISSUED_AT),
    )
    with pytest.raises(PolicyTTLExceededError):
        await application.create(policy_payload(expires_in_seconds=301))

    policy = make_policy()
    repository.records[policy.id] = policy
    policy.maximum_amount += 1
    with pytest.raises(PolicyIntegrityError):
        await application.get(policy.id)

    oversized_policy = make_policy()
    oversized_policy.allowed_currencies = [
        f"{chr(65 + first)}{chr(65 + second)}{chr(65 + third)}"
        for first in range(5)
        for second in range(5)
        for third in range(5)
    ][:101]
    repository.records[oversized_policy.id] = oversized_policy
    with pytest.raises(PolicyIntegrityError) as oversized_error:
        await application.get(oversized_policy.id)
    assert oversized_error.value.reason_code == "INTEGRITY_POLICY_DATA_INVALID"

    malformed_policy = make_policy()
    malformed_policy.allowed_currencies = {"INR": "ignored"}  # type: ignore[assignment]
    repository.records[malformed_policy.id] = malformed_policy
    with pytest.raises(PolicyIntegrityError) as malformed_error:
        await application.get(malformed_policy.id)
    assert malformed_error.value.reason_code == "INTEGRITY_POLICY_DATA_INVALID"


@pytest.mark.asyncio
async def test_evaluation_application_replays_evidence_at_original_evaluation_time() -> None:
    policy = make_policy()
    quote = make_quote()
    evaluation_repository = FakeEvaluationRepository()
    clock = FrozenClock(ISSUED_AT + timedelta(minutes=1))
    application = PolicyEvaluationApplicationService(  # type: ignore[arg-type]
        FakePolicyRepository([policy]),
        FakeQuoteRepository([quote]),
        evaluation_repository,
        clock=clock,
    )

    created = await application.create(
        PolicyEvaluationCreate(policy_id=POLICY_ID, quote_id=QUOTE_ID)
    )
    clock.current = policy.expires_at + timedelta(days=1)
    historical = await application.get(created.id)

    assert created.decision is PolicyDecision.ALLOW
    assert created.reason_codes == [PolicyReasonCode.ALLOW_POLICY_SATISFIED]
    assert created.created_at == created.evaluated_at
    assert historical == created


@pytest.mark.asyncio
async def test_evaluation_application_rejects_forged_persisted_evidence() -> None:
    policy = make_policy()
    quote = make_quote()
    evaluation_repository = FakeEvaluationRepository()
    application = PolicyEvaluationApplicationService(  # type: ignore[arg-type]
        FakePolicyRepository([policy]),
        FakeQuoteRepository([quote]),
        evaluation_repository,
        clock=FrozenClock(ISSUED_AT + timedelta(minutes=1)),
    )
    created = await application.create(
        PolicyEvaluationCreate(policy_id=POLICY_ID, quote_id=QUOTE_ID)
    )
    evaluation_repository.records[created.id].checks = evaluation_repository.records[
        created.id
    ].checks[:1]

    with pytest.raises(PolicyEvaluationIntegrityError):
        await application.get(created.id)


@pytest.mark.asyncio
async def test_evaluation_creation_rejects_a_persistence_timestamp_change() -> None:
    class TimestampMutatingRepository(FakeEvaluationRepository):
        async def create(self, evaluation: PolicyEvaluation) -> PolicyEvaluation:
            evaluation.evaluated_at += timedelta(seconds=1)
            return await super().create(evaluation)

    policy = make_policy()
    policy.maximum_amount += 1
    application = PolicyEvaluationApplicationService(  # type: ignore[arg-type]
        FakePolicyRepository([policy]),
        FakeQuoteRepository([make_quote()]),
        TimestampMutatingRepository(),  # type: ignore[arg-type]
        clock=FrozenClock(ISSUED_AT + timedelta(minutes=1)),
    )

    with pytest.raises(PolicyEvaluationIntegrityError):
        await application.create(PolicyEvaluationCreate(policy_id=POLICY_ID, quote_id=QUOTE_ID))


@pytest.mark.asyncio
async def test_evaluation_hash_mismatch_persists_deny_but_structural_failure_does_not() -> None:
    policy = make_policy()
    quote = make_quote()
    policy.maximum_amount += 1
    quote.amount += 1
    evaluation_repository = FakeEvaluationRepository()
    application = PolicyEvaluationApplicationService(  # type: ignore[arg-type]
        FakePolicyRepository([policy]),
        FakeQuoteRepository([quote]),
        evaluation_repository,
        clock=FrozenClock(ISSUED_AT + timedelta(minutes=1)),
    )

    denied = await application.create(
        PolicyEvaluationCreate(policy_id=POLICY_ID, quote_id=QUOTE_ID)
    )

    assert denied.decision is PolicyDecision.DENY
    assert denied.reason_codes == [
        PolicyReasonCode.INTEGRITY_POLICY_HASH_MISMATCH,
        PolicyReasonCode.INTEGRITY_QUOTE_HASH_MISMATCH,
    ]
    assert len(evaluation_repository.records) == 1

    policy.allowed_currencies = []
    with pytest.raises(PolicyIntegrityError):
        await application.create(PolicyEvaluationCreate(policy_id=POLICY_ID, quote_id=QUOTE_ID))
    assert len(evaluation_repository.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("future_resource", "tamper_hash", "error_type", "reason_code"),
    [
        ("policy", False, PolicyIntegrityError, "INTEGRITY_POLICY_DATA_INVALID"),
        ("policy", True, PolicyIntegrityError, "INTEGRITY_POLICY_DATA_INVALID"),
        ("quote", False, QuoteIntegrityError, "INTEGRITY_QUOTE_DATA_INVALID"),
        ("quote", True, QuoteIntegrityError, "INTEGRITY_QUOTE_DATA_INVALID"),
    ],
)
async def test_future_issued_source_fails_closed_without_persisting_evidence(
    future_resource: str,
    tamper_hash: bool,
    error_type: type[PolicyIntegrityError] | type[QuoteIntegrityError],
    reason_code: str,
) -> None:
    future_issue = ISSUED_AT + timedelta(seconds=1)
    policy = (
        make_policy(
            issued_at=future_issue,
            expires_at=future_issue + timedelta(minutes=15),
        )
        if future_resource == "policy"
        else make_policy()
    )
    quote = (
        make_quote(
            issued_at=future_issue,
            expires_at=future_issue + timedelta(minutes=5),
        )
        if future_resource == "quote"
        else make_quote()
    )
    if tamper_hash:
        if future_resource == "policy":
            policy.maximum_amount += 1
        else:
            quote.amount += 1
    evaluation_repository = FakeEvaluationRepository()
    application = PolicyEvaluationApplicationService(  # type: ignore[arg-type]
        FakePolicyRepository([policy]),
        FakeQuoteRepository([quote]),
        evaluation_repository,
        clock=FrozenClock(ISSUED_AT),
    )

    with pytest.raises(error_type) as error:
        await application.create(PolicyEvaluationCreate(policy_id=POLICY_ID, quote_id=QUOTE_ID))

    assert error.value.reason_code == reason_code
    assert evaluation_repository.records == {}


@pytest.mark.asyncio
async def test_evaluation_application_distinguishes_unknown_records() -> None:
    application = PolicyEvaluationApplicationService(  # type: ignore[arg-type]
        FakePolicyRepository(),
        FakeQuoteRepository(),
        FakeEvaluationRepository(),
        clock=FrozenClock(ISSUED_AT),
    )
    with pytest.raises(ResourceNotFoundError, match="Buyer policy"):
        await application.create(PolicyEvaluationCreate(policy_id=POLICY_ID, quote_id=QUOTE_ID))

    application = PolicyEvaluationApplicationService(  # type: ignore[arg-type]
        FakePolicyRepository([make_policy()]),
        FakeQuoteRepository(),
        FakeEvaluationRepository(),
        clock=FrozenClock(ISSUED_AT),
    )
    with pytest.raises(ResourceNotFoundError, match="Quote"):
        await application.create(PolicyEvaluationCreate(policy_id=POLICY_ID, quote_id=QUOTE_ID))
    with pytest.raises(ResourceNotFoundError, match="Policy evaluation"):
        await application.get(EVALUATION_ID)


@pytest.mark.asyncio
async def test_structurally_invalid_quote_returns_integrity_error_without_persistence() -> None:
    quote = make_quote()
    quote.service_snapshot = {"invalid": True}
    evaluation_repository = FakeEvaluationRepository()
    application = PolicyEvaluationApplicationService(  # type: ignore[arg-type]
        FakePolicyRepository([make_policy()]),
        FakeQuoteRepository([quote]),
        evaluation_repository,
        clock=FrozenClock(ISSUED_AT + timedelta(minutes=1)),
    )

    with pytest.raises(QuoteIntegrityError) as error:
        await application.create(PolicyEvaluationCreate(policy_id=POLICY_ID, quote_id=QUOTE_ID))

    assert error.value.reason_code == "INTEGRITY_QUOTE_DATA_INVALID"
    assert evaluation_repository.records == {}
