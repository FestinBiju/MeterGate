"""Byte-exact Razorpay checkout and webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Sequence

from app.providers.base import validate_provider_order_id, validate_provider_payment_id

_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _secret_bytes(secret: str) -> bytes:
    if (
        not isinstance(secret, str)
        or not 1 <= len(secret) <= 512
        or "\x00" in secret
        or secret != secret.strip()
    ):
        raise ValueError("Payment signature secret is invalid")
    return secret.encode("utf-8")


def checkout_signature_digest(*, order_id: str, payment_id: str, key_secret: str) -> str:
    """Return Razorpay's HMAC-SHA256 checkout digest using a trusted order ID."""
    validate_provider_order_id(order_id)
    validate_provider_payment_id(payment_id)
    message = f"{order_id}|{payment_id}".encode("ascii")
    return hmac.new(_secret_bytes(key_secret), message, hashlib.sha256).hexdigest()


def verify_checkout_signature(
    *,
    order_id: str,
    payment_id: str,
    signature: str,
    key_secret: str,
) -> bool:
    """Verify a Checkout result without trusting its browser-returned order ID."""
    if not isinstance(signature, str) or _SIGNATURE_PATTERN.fullmatch(signature) is None:
        return False
    try:
        expected = checkout_signature_digest(
            order_id=order_id,
            payment_id=payment_id,
            key_secret=key_secret,
        )
    except ValueError:
        return False
    return hmac.compare_digest(expected, signature)


def webhook_signature_digest(*, raw_body: bytes, webhook_secret: str) -> str:
    """Return the HMAC-SHA256 digest of the exact unmodified webhook bytes."""
    if type(raw_body) is not bytes:
        raise ValueError("Webhook signature input must be raw bytes")
    return hmac.new(_secret_bytes(webhook_secret), raw_body, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    *,
    raw_body: bytes,
    signature: str,
    webhook_secrets: Sequence[str],
) -> bool:
    """Verify against a bounded current/previous secret ring without short-circuiting."""
    if (
        type(raw_body) is not bytes
        or not isinstance(signature, str)
        or _SIGNATURE_PATTERN.fullmatch(signature) is None
        or isinstance(webhook_secrets, (str, bytes))
        or not 1 <= len(webhook_secrets) <= 3
    ):
        return False

    valid = False
    try:
        for secret in webhook_secrets:
            expected = webhook_signature_digest(raw_body=raw_body, webhook_secret=secret)
            valid = bool(valid | hmac.compare_digest(expected, signature))
    except ValueError:
        return False
    return valid
