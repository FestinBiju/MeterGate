"""Application operations for immutable buyer spending policies."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.domain.exceptions import (
    AuthenticationForbiddenError,
    PolicyIntegrityError,
    PolicyTTLExceededError,
    ResourceNotFoundError,
)
from app.domain.ids import new_policy_id
from app.domain.integrity import IntegrityStructureError
from app.domain.policy_hashing import (
    POLICY_VERSION,
    calculate_policy_hash,
    normalize_allowlist,
    verify_policy_integrity,
)
from app.models import BuyerPolicy
from app.repositories.buyer_policies import BuyerPolicyRepository
from app.schemas.policies import (
    BuyerPolicyCreate,
    BuyerPolicyResponse,
    PolicyConstraints,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class BuyerPolicyApplicationService:
    def __init__(
        self,
        repository: BuyerPolicyRepository,
        *,
        maximum_ttl: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        if not timedelta(seconds=1) <= maximum_ttl <= timedelta(days=1):
            raise ValueError("Maximum policy TTL must be between one second and one day")
        self._repository = repository
        self._maximum_ttl = maximum_ttl
        self._clock = clock

    async def create(
        self,
        payload: BuyerPolicyCreate,
        *,
        subject_ref: str,
    ) -> BuyerPolicyResponse:
        requested_ttl = timedelta(seconds=payload.expires_in_seconds)
        if requested_ttl > self._maximum_ttl:
            raise PolicyTTLExceededError(int(self._maximum_ttl.total_seconds()))

        issued_at = self._read_clock()
        expires_at = issued_at + requested_ttl
        policy_id = new_policy_id()
        constraints = self._normalized_constraints(payload)
        constraint_values = constraints.model_dump(mode="json")
        policy_hash = calculate_policy_hash(
            policy_id=policy_id,
            subject_ref=subject_ref,
            **constraint_values,
            issued_at=issued_at,
            expires_at=expires_at,
            policy_version=POLICY_VERSION,
        )
        policy = BuyerPolicy(
            id=policy_id,
            subject_ref=subject_ref,
            **constraint_values,
            issued_at=issued_at,
            expires_at=expires_at,
            policy_version=POLICY_VERSION,
            policy_hash=policy_hash,
        )
        persisted = await self._repository.create(policy)
        return self._to_response(persisted, now=self._read_clock())

    async def get(
        self,
        policy_id: str,
        *,
        owned_subject_refs: frozenset[str],
    ) -> BuyerPolicyResponse:
        policy = await self._repository.get(policy_id)
        if policy is None:
            raise ResourceNotFoundError("Buyer policy", policy_id)
        self._require_ownership(policy, owned_subject_refs)
        return self._to_response(policy, now=self._read_clock())

    @staticmethod
    def _require_ownership(
        policy: BuyerPolicy,
        owned_subject_refs: frozenset[str],
    ) -> None:
        if policy.subject_ref not in owned_subject_refs:
            raise AuthenticationForbiddenError(
                "The authenticated account does not own this buyer policy",
                "AUTH_RESOURCE_OWNERSHIP_MISMATCH",
            )

    @staticmethod
    def _normalized_constraints(payload: BuyerPolicyCreate) -> PolicyConstraints:
        return PolicyConstraints(
            maximum_amount=payload.maximum_amount,
            allowed_currencies=normalize_allowlist(payload.allowed_currencies),
            allowed_merchant_ids=normalize_allowlist(payload.allowed_merchant_ids),
            allowed_service_ids=normalize_allowlist(payload.allowed_service_ids),
            allowed_service_types=normalize_allowlist(payload.allowed_service_types),
            allowed_purchase_types=normalize_allowlist(payload.allowed_purchase_types),
        )

    def _to_response(self, policy: BuyerPolicy, *, now: datetime) -> BuyerPolicyResponse:
        try:
            verification = verify_policy_integrity(policy)
        except IntegrityStructureError as error:
            raise PolicyIntegrityError(policy.id, "INTEGRITY_POLICY_DATA_INVALID") from error
        issued_at = self._as_utc(policy.issued_at)
        expires_at = self._as_utc(policy.expires_at)
        if now < issued_at:
            raise PolicyIntegrityError(policy.id, "INTEGRITY_POLICY_DATA_INVALID")
        if not verification.hash_matches:
            raise PolicyIntegrityError(policy.id)
        return BuyerPolicyResponse(
            id=policy.id,
            subject_ref=policy.subject_ref,
            constraints=PolicyConstraints(
                maximum_amount=policy.maximum_amount,
                allowed_currencies=normalize_allowlist(policy.allowed_currencies),
                allowed_merchant_ids=normalize_allowlist(policy.allowed_merchant_ids),
                allowed_service_ids=normalize_allowlist(policy.allowed_service_ids),
                allowed_service_types=normalize_allowlist(policy.allowed_service_types),
                allowed_purchase_types=normalize_allowlist(policy.allowed_purchase_types),
            ),
            issued_at=issued_at,
            expires_at=expires_at,
            state="expired" if now >= expires_at else "active",
            policy_version=policy.policy_version,
            policy_hash=policy.policy_hash,
            created_at=policy.created_at,
        )

    def _read_clock(self) -> datetime:
        return self._as_utc(self._clock())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Policy timestamps must be timezone-aware")
        return value.astimezone(UTC)
