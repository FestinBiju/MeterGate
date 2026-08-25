"""Application orchestration for persisted deterministic policy evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError

from app.domain.enums import PolicyDecision
from app.domain.exceptions import (
    PolicyEvaluationIntegrityError,
    PolicyIntegrityError,
    QuoteIntegrityError,
    ResourceNotFoundError,
)
from app.domain.ids import new_policy_evaluation_id
from app.domain.integrity import IntegrityStructureError
from app.domain.policy_engine import (
    POLICY_EVALUATION_VERSION,
    PolicyCheckResult,
    PolicyEvaluationResult,
    PolicyReasonCode,
    evaluate_policy,
)
from app.models import PolicyEvaluation
from app.repositories.buyer_policies import BuyerPolicyRepository
from app.repositories.policy_evaluations import PolicyEvaluationRepository
from app.repositories.quotes import QuoteRepository
from app.schemas.policy_evaluations import (
    PolicyCheckResponse,
    PolicyEvaluationCreate,
    PolicyEvaluationResponse,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class PolicyEvaluationApplicationService:
    def __init__(
        self,
        policy_repository: BuyerPolicyRepository,
        quote_repository: QuoteRepository,
        evaluation_repository: PolicyEvaluationRepository,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._policy_repository = policy_repository
        self._quote_repository = quote_repository
        self._evaluation_repository = evaluation_repository
        self._clock = clock

    async def create(self, payload: PolicyEvaluationCreate) -> PolicyEvaluationResponse:
        policy = await self._policy_repository.get(payload.policy_id)
        if policy is None:
            raise ResourceNotFoundError("Buyer policy", payload.policy_id)
        quote = await self._quote_repository.get(payload.quote_id)
        if quote is None:
            raise ResourceNotFoundError("Quote", payload.quote_id)

        evaluated_at = self._read_clock()
        try:
            result = evaluate_policy(policy, quote, evaluated_at)
        except IntegrityStructureError as error:
            if error.resource == "policy":
                raise PolicyIntegrityError(
                    policy.id,
                    "INTEGRITY_POLICY_DATA_INVALID",
                ) from error
            raise QuoteIntegrityError(quote.id, "INTEGRITY_QUOTE_DATA_INVALID") from error

        evaluation = PolicyEvaluation(
            id=new_policy_evaluation_id(),
            policy_id=policy.id,
            quote_id=quote.id,
            policy_hash=policy.policy_hash,
            quote_hash=quote.quote_hash,
            decision=result.decision,
            checks=[check.as_dict() for check in result.checks],
            evaluated_at=evaluated_at,
            evaluation_version=POLICY_EVALUATION_VERSION,
        )
        persisted = await self._evaluation_repository.create(evaluation)
        return self._to_response(
            persisted,
            known_result=result,
            expected_policy_id=policy.id,
            expected_quote_id=quote.id,
            expected_policy_hash=policy.policy_hash,
            expected_quote_hash=quote.quote_hash,
            expected_evaluated_at=evaluated_at,
        )

    async def get(self, evaluation_id: str) -> PolicyEvaluationResponse:
        evaluation = await self._evaluation_repository.get(evaluation_id)
        if evaluation is None:
            raise ResourceNotFoundError("Policy evaluation", evaluation_id)

        policy = await self._policy_repository.get(evaluation.policy_id)
        quote = await self._quote_repository.get(evaluation.quote_id)
        if policy is None or quote is None:
            raise PolicyEvaluationIntegrityError(evaluation.id)
        try:
            evaluated_at = self._as_utc(evaluation.evaluated_at)
            result = evaluate_policy(policy, quote, evaluated_at)
        except (IntegrityStructureError, TypeError, ValueError) as error:
            raise PolicyEvaluationIntegrityError(evaluation.id) from error

        return self._to_response(
            evaluation,
            known_result=result,
            expected_policy_id=policy.id,
            expected_quote_id=quote.id,
            expected_policy_hash=policy.policy_hash,
            expected_quote_hash=quote.quote_hash,
            expected_evaluated_at=evaluated_at,
        )

    @staticmethod
    def _to_response(
        evaluation: PolicyEvaluation,
        *,
        known_result: PolicyEvaluationResult,
        expected_policy_id: str,
        expected_quote_id: str,
        expected_policy_hash: str,
        expected_quote_hash: str,
        expected_evaluated_at: datetime,
    ) -> PolicyEvaluationResponse:
        try:
            evaluated_at = PolicyEvaluationApplicationService._as_utc(evaluation.evaluated_at)
            checks = [PolicyCheckResponse.model_validate(check) for check in evaluation.checks]
            decision = PolicyDecision(evaluation.decision)
            reason_codes = _derive_reason_codes(decision, checks)
            expected_checks = [check.as_dict() for check in known_result.checks]
            stored_checks = [check.model_dump(mode="json") for check in checks]
            expected_reason_codes = list(known_result.reason_codes)
            if (
                evaluation.policy_id != expected_policy_id
                or evaluation.quote_id != expected_quote_id
                or evaluation.policy_hash != expected_policy_hash
                or evaluation.quote_hash != expected_quote_hash
                or evaluated_at != expected_evaluated_at
                or evaluation.evaluation_version != POLICY_EVALUATION_VERSION
                or decision != known_result.decision
                or stored_checks != expected_checks
                or reason_codes != expected_reason_codes
            ):
                raise ValueError("Stored evaluation does not match deterministic evidence")
            return PolicyEvaluationResponse(
                id=evaluation.id,
                policy_id=evaluation.policy_id,
                quote_id=evaluation.quote_id,
                policy_hash=evaluation.policy_hash,
                quote_hash=evaluation.quote_hash,
                decision=decision,
                reason_codes=reason_codes,
                checks=checks,
                evaluated_at=evaluated_at,
                evaluation_version=evaluation.evaluation_version,
                created_at=evaluation.created_at,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise PolicyEvaluationIntegrityError(evaluation.id) from error

    def _read_clock(self) -> datetime:
        return self._as_utc(self._clock())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Policy evaluation time must be timezone-aware")
        return value.astimezone(UTC)


def _derive_reason_codes(
    decision: PolicyDecision,
    checks: list[PolicyCheckResponse],
) -> list[PolicyReasonCode]:
    failures = [check.reason_code for check in checks if check.result is PolicyCheckResult.FAIL]
    if decision is PolicyDecision.ALLOW:
        if failures:
            raise ValueError("Allow evaluation contains failing checks")
        return [PolicyReasonCode.ALLOW_POLICY_SATISFIED]
    if not failures:
        raise ValueError("Deny evaluation contains no failing checks")
    return failures
