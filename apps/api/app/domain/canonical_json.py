"""RFC 8785 canonical JSON for durable commerce integrity bindings."""

from typing import Any

import rfc8785


class CanonicalJSONError(ValueError):
    """Raised when a value cannot be represented by RFC 8785."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the RFC 8785 JSON Canonicalization Scheme representation."""
    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, UnicodeError) as error:
        raise CanonicalJSONError("Value cannot be represented as canonical JSON") from error
