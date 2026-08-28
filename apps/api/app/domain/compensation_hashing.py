"""Canonical integrity hash for immutable fulfillment-failure evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.domain.hashing import canonical_utc_datetime, sha256_json

FAILURE_EVIDENCE_VERSION = "1"


class FailureEvidenceFields(Protocol):
    """Persisted fulfillment fields needed to recompute failure evidence."""

    id: str
    transaction_id: str
    entitlement_id: str
    service_id: str
    input_hash: str
    attempt_count: int
    failure_code: str | None
    result_content_type: str | None
    result_json: Any | None
    result_hash: str | None
    result_size_bytes: int | None
    started_at: datetime | None
    failed_at: datetime | None


def build_failure_evidence_payload(
    *,
    fulfillment_execution_id: str,
    transaction_id: str,
    entitlement_id: str,
    failure_code: str,
    attempt_count: int,
    service_id: str,
    input_hash: str,
    result_absent: bool,
    started_at: datetime,
    failed_at: datetime,
) -> dict[str, Any]:
    """Build the versioned canonical material bound to a compensation case."""
    if not failure_code:
        raise ValueError("Failure evidence requires a failure code")
    if attempt_count < 1:
        raise ValueError("Failure evidence requires at least one fulfillment attempt")
    if not result_absent:
        raise ValueError("Compensation failure evidence cannot claim delivered result material")
    if failed_at < started_at:
        raise ValueError("Failure evidence timestamp precedes fulfillment start")
    return {
        "version": FAILURE_EVIDENCE_VERSION,
        "fulfillment_execution_id": fulfillment_execution_id,
        "transaction_id": transaction_id,
        "entitlement_id": entitlement_id,
        "failure_code": failure_code,
        "attempt_count": attempt_count,
        "service_id": service_id,
        "input_hash": input_hash,
        "result_absent": result_absent,
        "started_at": canonical_utc_datetime(started_at),
        "failed_at": canonical_utc_datetime(failed_at),
    }


def calculate_failure_evidence_hash(**fields: Any) -> str:
    """Hash explicitly supplied immutable failure evidence using RFC 8785."""
    return sha256_json(build_failure_evidence_payload(**fields))


def recompute_failure_evidence_hash(execution: FailureEvidenceFields) -> str:
    """Recompute a failure hash from one persisted terminal fulfillment."""
    if (
        execution.failure_code is None
        or execution.started_at is None
        or execution.failed_at is None
    ):
        raise ValueError("Fulfillment does not contain complete terminal failure evidence")
    result_absent = all(
        value is None
        for value in (
            execution.result_content_type,
            execution.result_json,
            execution.result_hash,
            execution.result_size_bytes,
        )
    )
    return calculate_failure_evidence_hash(
        fulfillment_execution_id=execution.id,
        transaction_id=execution.transaction_id,
        entitlement_id=execution.entitlement_id,
        failure_code=execution.failure_code,
        attempt_count=execution.attempt_count,
        service_id=execution.service_id,
        input_hash=execution.input_hash,
        result_absent=result_absent,
        started_at=execution.started_at,
        failed_at=execution.failed_at,
    )
