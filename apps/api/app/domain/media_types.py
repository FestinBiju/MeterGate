"""Media-type rules for the bounded JSON fulfillment snapshot contract."""

from __future__ import annotations

import re

_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


def normalize_json_content_type(value: object) -> str | None:
    """Return a canonical JSON media type, or None for unsupported content."""
    if not isinstance(value, str):
        return None
    normalized = value.lower()
    if _MEDIA_TYPE.fullmatch(normalized) is None:
        return None
    if normalized == "application/json" or (
        normalized.startswith("application/") and normalized.endswith("+json")
    ):
        return normalized
    return None
