"""Shared validation primitives for the versioned domain API."""

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints

Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
Description = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000),
]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
OutputContentType = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        max_length=255,
        pattern=(
            r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/"
            r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
        ),
    ),
]
JSONObject = dict[str, Any]
AccountId = Annotated[
    str,
    StringConstraints(pattern=r"^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]


class APIModel(BaseModel):
    """Reject undeclared fields across all public domain contracts."""

    model_config = ConfigDict(extra="forbid")


class ORMResponseModel(APIModel):
    """Allow explicit response models to read canonical ORM attributes."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


def reject_empty_or_null_patch(data: Any) -> Any:
    """Require PATCH documents to contain only explicit, non-null updates."""
    if not isinstance(data, dict):
        return data
    if not data:
        raise ValueError("PATCH body must contain at least one field")
    null_fields = sorted(key for key, value in data.items() if value is None)
    if null_fields:
        raise ValueError(f"PATCH fields cannot be null: {', '.join(null_fields)}")
    return data
