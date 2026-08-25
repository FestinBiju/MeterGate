"""Strict unpadded base64url encoding for WebAuthn binary fields."""

from __future__ import annotations

from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error


def encode_base64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_base64url(value: str, *, maximum_bytes: int = 4_096) -> bytes:
    """Decode one canonical, unpadded base64url value or fail closed."""
    if not isinstance(value, str) or not value or "=" in value:
        raise ValueError("Invalid base64url value")
    try:
        encoded = value.encode("ascii")
        decoded = b64decode(
            encoded + b"=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (Base64Error, UnicodeEncodeError, ValueError) as error:
        raise ValueError("Invalid base64url value") from error
    if not decoded or len(decoded) > maximum_bytes or encode_base64url(decoded) != value:
        raise ValueError("Invalid base64url value")
    return decoded
