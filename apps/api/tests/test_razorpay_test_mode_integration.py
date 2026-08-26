"""Explicitly opt-in, side-effecting acceptance against Razorpay Test Mode."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.domain.ids import new_payment_transaction_id
from app.providers import CreateProviderOrder, RazorpayPaymentProvider

_RECEIPT_INDEX_ATTEMPTS = 30


@pytest.mark.asyncio
async def test_real_razorpay_test_mode_order_create_fetch_and_receipt_lookup() -> None:
    """Create one real Test Mode Order only when a human explicitly opts in."""
    if os.getenv("RUN_RAZORPAY_TEST_MODE") != "1":
        pytest.skip("set RUN_RAZORPAY_TEST_MODE=1 to create a real Test Mode Order")

    settings = Settings()
    assert settings.razorpay_key_id is not None, "RAZORPAY_KEY_ID is required"
    assert settings.razorpay_key_secret is not None, "RAZORPAY_KEY_SECRET is required"
    provider = RazorpayPaymentProvider(
        key_id=settings.razorpay_key_id.get_secret_value(),
        key_secret=settings.razorpay_key_secret.get_secret_value(),
        connect_timeout_seconds=settings.razorpay_connect_timeout_seconds,
        read_timeout_seconds=settings.razorpay_read_timeout_seconds,
        max_concurrency=1,
    )
    receipt = new_payment_transaction_id()
    started_at = datetime.now(UTC)
    created = await provider.create_order(
        CreateProviderOrder(
            amount=500,
            currency="INR",
            receipt=receipt,
            notes={
                "metergate_transaction_id": receipt,
                "acceptance": "milestone_6b_opt_in",
            },
        )
    )
    fetched = await provider.fetch_order(created.id)
    matching = ()
    for lookup_attempt in range(_RECEIPT_INDEX_ATTEMPTS):
        matching = await provider.find_orders_by_receipt(
            receipt,
            created_from=started_at - timedelta(minutes=5),
            created_to=datetime.now(UTC) + timedelta(minutes=5),
        )
        if matching:
            break
        if lookup_attempt < _RECEIPT_INDEX_ATTEMPTS - 1:
            # Razorpay's receipt-filtered list can lag the immediately consistent
            # direct Order fetch.  Production remains uncertain and is retried by
            # later reconciliation; this opt-in test gives indexing a bounded wait.
            await asyncio.sleep(1)

    assert created.status == "created"
    assert created.receipt == receipt
    assert created.amount == 500
    assert created.currency == "INR"
    assert fetched == created
    assert matching == (created,)
    print(f"Real Razorpay Test Mode Order accepted: {created.id}")
