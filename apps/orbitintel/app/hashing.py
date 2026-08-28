"""RFC 8785 hashing for exact input and internal idempotency bindings."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import rfc8785


def canonical_json_bytes(value: object) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError) as error:
        raise ValueError("Value is not valid RFC 8785 JSON") from error


def sha256_prefixed(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def canonical_json_hash(value: object) -> str:
    return sha256_prefixed(canonical_json_bytes(value))


def fulfillment_request_hash(
    *,
    fulfillment_execution_id: str,
    service_id: str,
    service_slug: str,
    input_value: Mapping[str, Any],
    input_hash: str,
) -> str:
    return canonical_json_hash(
        {
            "fulfillment_execution_id": fulfillment_execution_id,
            "input": dict(input_value),
            "input_hash": input_hash,
            "request_version": "1",
            "service_id": service_id,
            "service_slug": service_slug,
        }
    )
