"""Explicitly opt-in, side-effecting refund acceptance against Razorpay Test Mode."""

from __future__ import annotations

import asyncio
import os
import secrets

import pytest

from app.core.config import Settings
from app.providers import CreateProviderRefund, RazorpayPaymentProvider

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_REFUND_INDEX_ATTEMPTS = 30
_REFUND_TERMINAL_ATTEMPTS = 30


def _new_refund_receipt() -> str:
    return (
        "rfd_"
        + secrets.choice("01234567")
        + "".join(secrets.choice(_CROCKFORD_ALPHABET) for _ in range(25))
    )


@pytest.mark.asyncio
async def test_real_razorpay_test_mode_refund_create_fetch_and_payment_list() -> None:
    """Refund one configured captured Test Mode Payment only after explicit opt-in."""
    if os.getenv("RUN_RAZORPAY_REFUND_TEST_MODE") != "1":
        pytest.skip(
            "set RUN_RAZORPAY_REFUND_TEST_MODE=1 and RAZORPAY_REFUND_TEST_PAYMENT_ID "
            "to create a real Test Mode refund"
        )

    payment_id = os.getenv("RAZORPAY_REFUND_TEST_PAYMENT_ID")
    assert payment_id, "RAZORPAY_REFUND_TEST_PAYMENT_ID is required; never hardcode a payment ID"

    settings = Settings()
    assert settings.razorpay_key_id is not None, "RAZORPAY_KEY_ID is required"
    assert settings.razorpay_key_secret is not None, "RAZORPAY_KEY_SECRET is required"
    key_id = settings.razorpay_key_id.get_secret_value()
    assert key_id.startswith("rzp_test_"), (
        "REFUSING REFUND: RAZORPAY_KEY_ID must be a Razorpay Test Mode key"
    )

    provider = RazorpayPaymentProvider(
        key_id=key_id,
        key_secret=settings.razorpay_key_secret.get_secret_value(),
        connect_timeout_seconds=settings.razorpay_connect_timeout_seconds,
        read_timeout_seconds=settings.razorpay_read_timeout_seconds,
        max_concurrency=1,
    )
    payment = await provider.fetch_payment(payment_id)
    refundable_amount = payment.amount - payment.amount_refunded
    assert payment.captured, "configured Test Mode payment must be captured"
    assert refundable_amount > 0, "configured Test Mode payment has no refundable balance"

    receipt = _new_refund_receipt()
    print(
        "WARNING: issuing a real Razorpay Test Mode refund "
        f"for payment {payment.id}, amount {refundable_amount} {payment.currency}"
    )
    created = await provider.create_refund(
        CreateProviderRefund(
            payment_id=payment.id,
            amount=refundable_amount,
            currency=payment.currency,
            receipt=receipt,
            notes={
                "metergate_refund_id": receipt,
                "acceptance": "milestone_8_opt_in",
            },
        )
    )
    fetched = created
    for status_attempt in range(_REFUND_TERMINAL_ATTEMPTS):
        fetched = await provider.fetch_refund(payment.id, created.id)
        if fetched.status in {"processed", "failed"}:
            break
        if status_attempt < _REFUND_TERMINAL_ATTEMPTS - 1:
            await asyncio.sleep(1)
    listed = ()
    for lookup_attempt in range(_REFUND_INDEX_ATTEMPTS):
        listed = await provider.fetch_refunds_for_payment(payment.id)
        if any(refund.id == created.id for refund in listed):
            break
        if lookup_attempt < _REFUND_INDEX_ATTEMPTS - 1:
            await asyncio.sleep(1)

    assert created.payment_id == payment.id
    assert created.amount == refundable_amount
    assert created.receipt == receipt
    assert created.status in {"pending", "processed"}
    assert fetched.id == created.id
    assert fetched.payment_id == created.payment_id
    assert fetched.amount == created.amount
    assert fetched.receipt == created.receipt
    assert fetched.currency in {None, payment.currency}
    assert fetched.status == "processed", (
        f"real Test Mode refund did not reach processed; final status={fetched.status}"
    )
    assert any(refund.id == created.id for refund in listed)
    print(f"Real Razorpay Test Mode refund completed: {created.id} ({fetched.status})")
