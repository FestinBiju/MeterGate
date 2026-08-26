"""One canonical validation path for quoted and protected service input."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.domain.canonical_json import CanonicalJSONError, canonical_json_bytes
from app.domain.hashing import sha256_bytes
from app.domain.json_schema import (
    JSONSchemaConfigurationError,
    validate_json_instance,
)


@dataclass(frozen=True, slots=True)
class CanonicalServiceInput:
    """Schema-validated JSON value plus its RFC 8785 representation and hash."""

    value: Any
    canonical_bytes: bytes
    input_hash: str


class ServiceInputConfigurationError(ValueError):
    """The merchant's persisted input schema is not safe to evaluate."""


class ServiceInputValueError(ValueError):
    """The caller's input does not satisfy the service schema or canonical JSON."""

    def __init__(self, violations: tuple[tuple[tuple[str | int, ...], str], ...]) -> None:
        self.violations = violations
        super().__init__("Service input is invalid")


def validate_normalize_and_hash_service_input(
    value: Any,
    schema: dict[str, Any],
) -> CanonicalServiceInput:
    """Validate once, normalize through RFC 8785, and return the exact input hash."""
    try:
        violations = validate_json_instance(value, schema)
    except JSONSchemaConfigurationError as error:
        raise ServiceInputConfigurationError("Service input schema is invalid") from error
    if violations:
        raise ServiceInputValueError(
            tuple((violation.path, violation.keyword) for violation in violations)
        )
    try:
        canonical = canonical_json_bytes(value)
        normalized = json.loads(canonical)
    except (CanonicalJSONError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceInputValueError((((), "canonicalization"),)) from error
    return CanonicalServiceInput(
        value=normalized,
        canonical_bytes=canonical,
        input_hash=sha256_bytes(canonical),
    )
