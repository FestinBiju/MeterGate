"""Bounded Draft 2020-12 validation for merchant-defined JSON Schemas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_SUPPORTED_DIALECTS = frozenset({JSON_SCHEMA_DIALECT, f"{JSON_SCHEMA_DIALECT}#"})
_REFERENCE_KEYWORDS = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})
_REGEX_KEYWORDS = frozenset({"pattern", "patternProperties"})
_SCHEMA_MAP_KEYWORDS = frozenset(
    {"$defs", "definitions", "dependentSchemas", "properties", "patternProperties"}
)
_SCHEMA_SINGLE_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_COMBINATOR_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf"})
_SUPPORTED_FORMATS = frozenset({"date-time"})
_RFC3339_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time")
def _is_rfc3339_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if _RFC3339_DATE_TIME.fullmatch(value) is None:
        return False
    normalized = value[:-1] + "+00:00" if value[-1] in {"Z", "z"} else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


MAX_VALIDATION_ISSUES = 20
MAX_SCHEMA_NODES = 2_048
MAX_SCHEMA_DEPTH = 32
MAX_SCHEMA_TEXT_BYTES = 128 * 1_024
MAX_COMBINATOR_BRANCHES = 64
MAX_COMBINATOR_DEPTH = 4
MAX_COMBINATOR_WIDTH = 16
MAX_INSTANCE_NODES = 4_096
MAX_INSTANCE_DEPTH = 64
MAX_INSTANCE_TEXT_BYTES = 256 * 1_024


class JSONSchemaConfigurationError(ValueError):
    """A stored schema is invalid or depends on an external resource."""


@dataclass(frozen=True, slots=True)
class JSONSchemaViolation:
    path: tuple[str | int, ...]
    keyword: str


def validate_json_schema_document(schema: dict[str, Any]) -> None:
    """Require one self-contained Draft 2020-12 schema object."""
    if not isinstance(schema, dict):
        raise JSONSchemaConfigurationError("JSON Schema must be an object")

    if not _json_shape_is_within_limits(
        schema,
        max_nodes=MAX_SCHEMA_NODES,
        max_depth=MAX_SCHEMA_DEPTH,
        max_text_bytes=MAX_SCHEMA_TEXT_BYTES,
    ):
        raise JSONSchemaConfigurationError("JSON Schema exceeds complexity limits")

    _validate_schema_safety(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except (RecursionError, SchemaError) as error:
        raise JSONSchemaConfigurationError("Invalid Draft 2020-12 JSON Schema") from error


def validate_json_instance(
    instance: Any,
    schema: dict[str, Any],
) -> tuple[JSONSchemaViolation, ...]:
    """Return deterministic, value-free validation issues for an instance."""
    validate_json_schema_document(schema)
    if not _json_shape_is_within_limits(
        instance,
        max_nodes=MAX_INSTANCE_NODES,
        max_depth=MAX_INSTANCE_DEPTH,
        max_text_bytes=MAX_INSTANCE_TEXT_BYTES,
    ):
        return (JSONSchemaViolation(path=(), keyword="complexity"),)

    validator = Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)
    try:
        errors = list(islice(validator.iter_errors(instance), MAX_VALIDATION_ISSUES))
        errors.sort(key=_error_sort_key)
    except (RecursionError, Unresolvable) as error:
        raise JSONSchemaConfigurationError(
            "JSON Schema contains an unresolved reference"
        ) from error

    return tuple(
        JSONSchemaViolation(
            path=tuple(
                segment if isinstance(segment, (str, int)) else str(segment)
                for segment in error.absolute_path
            ),
            keyword=str(error.validator or "schema"),
        )
        for error in errors
    )


def _validate_schema_safety(schema: dict[str, Any]) -> None:
    stack: list[tuple[dict[str, Any] | bool, int]] = [(schema, 0)]
    combinator_branches = 0

    while stack:
        value, combinator_depth = stack.pop()
        if isinstance(value, bool):
            continue

        dialect = value.get("$schema")
        if dialect is not None and dialect not in _SUPPORTED_DIALECTS:
            raise JSONSchemaConfigurationError("Unsupported JSON Schema dialect")

        declared_format = value.get("format")
        if declared_format is not None and declared_format not in _SUPPORTED_FORMATS:
            raise JSONSchemaConfigurationError("Unsupported JSON Schema format")

        if _REGEX_KEYWORDS.intersection(value):
            raise JSONSchemaConfigurationError(
                "Regex JSON Schema keywords are not supported for quote inputs"
            )

        for keyword in _REFERENCE_KEYWORDS:
            reference = value.get(keyword)
            if isinstance(reference, str):
                if reference in {"", "#"}:
                    raise JSONSchemaConfigurationError(
                        "Self-only JSON Schema references are not allowed"
                    )
                if not reference.startswith("#"):
                    raise JSONSchemaConfigurationError(
                        "External JSON Schema references are not allowed"
                    )

        for keyword in _SCHEMA_MAP_KEYWORDS:
            children = value.get(keyword)
            if isinstance(children, dict):
                stack.extend(
                    (child, combinator_depth)
                    for child in children.values()
                    if isinstance(child, (bool, dict))
                )

        for keyword in _SCHEMA_SINGLE_KEYWORDS:
            child = value.get(keyword)
            if isinstance(child, (bool, dict)):
                stack.append((child, combinator_depth))

        for keyword in _SCHEMA_LIST_KEYWORDS:
            children = value.get(keyword)
            if not isinstance(children, list):
                continue
            child_depth = combinator_depth
            if keyword in _COMBINATOR_KEYWORDS:
                combinator_branches += len(children)
                child_depth += 1
                if (
                    len(children) > MAX_COMBINATOR_WIDTH
                    or combinator_branches > MAX_COMBINATOR_BRANCHES
                    or child_depth > MAX_COMBINATOR_DEPTH
                ):
                    raise JSONSchemaConfigurationError(
                        "JSON Schema combinators exceed complexity limits"
                    )
            stack.extend(
                (child, child_depth) for child in children if isinstance(child, (bool, dict))
            )


def _json_shape_is_within_limits(
    value: Any,
    *,
    max_nodes: int,
    max_depth: int,
    max_text_bytes: int,
) -> bool:
    nodes = 0
    text_bytes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]

    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return False

        if isinstance(current, str):
            text_bytes += len(current.encode("utf-8"))
        elif isinstance(current, dict):
            for key, child in current.items():
                text_bytes += len(str(key).encode("utf-8"))
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)

        if text_bytes > max_text_bytes:
            return False

    return True


def _error_sort_key(error: Any) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    instance_path = tuple(f"{type(item).__name__}:{item}" for item in error.absolute_path)
    schema_path = tuple(f"{type(item).__name__}:{item}" for item in error.absolute_schema_path)
    return instance_path, schema_path, str(error.validator or "schema")
