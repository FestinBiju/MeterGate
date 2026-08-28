import pytest

from app.domain.enums import (
    PaymentAttemptStatus,
    PaymentTransactionState,
    RazorpayOrderStatus,
)
from app.domain.payment_state import (
    can_transition_attempt_status,
    can_transition_order_status,
    can_transition_transaction,
    require_transaction_transition,
)


@pytest.mark.parametrize(
    ("previous", "next_state"),
    [
        ("order_creation_pending", "order_created"),
        ("order_creation_pending", "order_creation_failed"),
        ("order_creation_failed", "order_creation_pending"),
        ("order_creation_uncertain", "order_created"),
        ("order_created", "payment_pending"),
        ("order_created", "payment_authorized"),
        ("payment_pending", "paid"),
        ("payment_authorized", "paid"),
        ("reconciliation_required", "payment_authorized"),
        ("paid", "paid"),
    ],
)
def test_transaction_state_accepts_legal_monotonic_transitions(
    previous: str,
    next_state: str,
) -> None:
    assert can_transition_transaction(previous, next_state)
    require_transaction_transition(previous, next_state)


@pytest.mark.parametrize(
    ("previous", "next_state"),
    [
        ("paid", "payment_authorized"),
        ("payment_authorized", "payment_pending"),
        ("order_created", "order_creation_pending"),
        ("order_creation_failed", "order_created"),
    ],
)
def test_transaction_state_rejects_regression_and_unsafe_shortcuts(
    previous: str,
    next_state: str,
) -> None:
    assert not can_transition_transaction(previous, next_state)
    with pytest.raises(ValueError, match="is not allowed"):
        require_transaction_transition(previous, next_state)


def test_order_status_is_monotonic_and_allows_first_observation() -> None:
    assert can_transition_order_status(None, RazorpayOrderStatus.PAID)
    assert can_transition_order_status("created", "attempted")
    assert can_transition_order_status("created", "paid")
    assert can_transition_order_status("attempted", "paid")
    assert can_transition_order_status("paid", "paid")
    assert not can_transition_order_status("paid", "attempted")
    assert not can_transition_order_status("attempted", None)


def test_attempt_state_allows_late_authorization_but_never_capture_regression() -> None:
    assert can_transition_attempt_status("failed", "authorized")
    assert can_transition_attempt_status("failed", "captured")
    assert can_transition_attempt_status("authorized", "captured")
    assert can_transition_attempt_status("captured", "refunded")
    assert can_transition_attempt_status(
        PaymentAttemptStatus.CAPTURED,
        PaymentAttemptStatus.CAPTURED,
    )
    assert not can_transition_attempt_status("captured", "authorized")
    assert not can_transition_attempt_status("captured", "failed")
    assert not can_transition_attempt_status("refunded", "captured")


def test_state_helpers_reject_unknown_persisted_values() -> None:
    with pytest.raises(ValueError):
        can_transition_transaction("unknown", PaymentTransactionState.PAID)
    with pytest.raises(ValueError):
        can_transition_order_status("created", "unknown")
    with pytest.raises(ValueError):
        can_transition_attempt_status("created", "unknown")
