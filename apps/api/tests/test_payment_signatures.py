from __future__ import annotations

import hashlib
import hmac

import pytest

from app.providers.signatures import (
    checkout_signature_digest,
    verify_checkout_signature,
    verify_webhook_signature,
    webhook_signature_digest,
)

ORDER_ID = "order_ABC123"
PAYMENT_ID = "pay_XYZ789"
KEY_SECRET = "checkout-secret"
WEBHOOK_SECRET = "webhook-secret"


def test_checkout_signature_uses_trusted_order_and_payment_ids() -> None:
    expected = hmac.new(
        KEY_SECRET.encode(),
        f"{ORDER_ID}|{PAYMENT_ID}".encode(),
        hashlib.sha256,
    ).hexdigest()

    assert (
        checkout_signature_digest(
            order_id=ORDER_ID,
            payment_id=PAYMENT_ID,
            key_secret=KEY_SECRET,
        )
        == expected
    )
    assert verify_checkout_signature(
        order_id=ORDER_ID,
        payment_id=PAYMENT_ID,
        signature=expected,
        key_secret=KEY_SECRET,
    )
    assert not verify_checkout_signature(
        order_id="order_DIFFERENT",
        payment_id=PAYMENT_ID,
        signature=expected,
        key_secret=KEY_SECRET,
    )


@pytest.mark.parametrize("signature", ["", "g" * 64, "0" * 63, "0" * 65])
def test_checkout_signature_rejects_malformed_hex(signature: str) -> None:
    assert not verify_checkout_signature(
        order_id=ORDER_ID,
        payment_id=PAYMENT_ID,
        signature=signature,
        key_secret=KEY_SECRET,
    )


def test_webhook_signature_is_over_exact_raw_bytes_and_supports_rotation() -> None:
    raw_body = b'{"event":"payment.captured","payload":{"amount":100}}'
    digest = webhook_signature_digest(raw_body=raw_body, webhook_secret=WEBHOOK_SECRET)

    assert verify_webhook_signature(
        raw_body=raw_body,
        signature=digest,
        webhook_secrets=("previous-secret", WEBHOOK_SECRET),
    )
    assert not verify_webhook_signature(
        raw_body=b'{"payload":{"amount":100},"event":"payment.captured"}',
        signature=digest,
        webhook_secrets=(WEBHOOK_SECRET,),
    )
    assert not verify_webhook_signature(
        raw_body=raw_body + b"\n",
        signature=digest,
        webhook_secrets=(WEBHOOK_SECRET,),
    )


def test_webhook_signature_rejects_text_bodies_and_unbounded_secret_rings() -> None:
    with pytest.raises(ValueError, match="raw bytes"):
        webhook_signature_digest(  # type: ignore[arg-type]
            raw_body="{}",
            webhook_secret=WEBHOOK_SECRET,
        )

    assert not verify_webhook_signature(
        raw_body=b"{}",
        signature="0" * 64,
        webhook_secrets=(),
    )
    assert not verify_webhook_signature(
        raw_body=b"{}",
        signature="0" * 64,
        webhook_secrets=("a", "b", "c", "d"),
    )
