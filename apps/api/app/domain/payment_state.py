"""Legal monotonic state transitions for Razorpay payment evidence."""

from app.domain.enums import (
    PaymentAttemptStatus,
    PaymentTransactionState,
    RazorpayOrderStatus,
)

_TRANSACTION_TRANSITIONS: dict[
    PaymentTransactionState,
    frozenset[PaymentTransactionState],
] = {
    PaymentTransactionState.ORDER_CREATION_PENDING: frozenset(
        {
            PaymentTransactionState.ORDER_CREATED,
            PaymentTransactionState.ORDER_CREATION_FAILED,
            PaymentTransactionState.ORDER_CREATION_UNCERTAIN,
            PaymentTransactionState.RECONCILIATION_REQUIRED,
        }
    ),
    PaymentTransactionState.ORDER_CREATION_FAILED: frozenset(
        {
            PaymentTransactionState.ORDER_CREATION_PENDING,
            PaymentTransactionState.RECONCILIATION_REQUIRED,
        }
    ),
    PaymentTransactionState.ORDER_CREATION_UNCERTAIN: frozenset(
        {
            PaymentTransactionState.ORDER_CREATION_PENDING,
            PaymentTransactionState.ORDER_CREATED,
            PaymentTransactionState.RECONCILIATION_REQUIRED,
        }
    ),
    PaymentTransactionState.ORDER_CREATED: frozenset(
        {
            PaymentTransactionState.PAYMENT_PENDING,
            PaymentTransactionState.PAYMENT_AUTHORIZED,
            PaymentTransactionState.PAID,
            PaymentTransactionState.RECONCILIATION_REQUIRED,
        }
    ),
    PaymentTransactionState.PAYMENT_PENDING: frozenset(
        {
            PaymentTransactionState.PAYMENT_AUTHORIZED,
            PaymentTransactionState.PAID,
            PaymentTransactionState.RECONCILIATION_REQUIRED,
        }
    ),
    PaymentTransactionState.PAYMENT_AUTHORIZED: frozenset(
        {
            PaymentTransactionState.PAID,
            PaymentTransactionState.RECONCILIATION_REQUIRED,
        }
    ),
    PaymentTransactionState.PAID: frozenset(),
    PaymentTransactionState.RECONCILIATION_REQUIRED: frozenset(
        {
            PaymentTransactionState.ORDER_CREATION_PENDING,
            PaymentTransactionState.ORDER_CREATED,
            PaymentTransactionState.PAYMENT_PENDING,
            PaymentTransactionState.PAYMENT_AUTHORIZED,
            PaymentTransactionState.PAID,
        }
    ),
}

_ORDER_TRANSITIONS: dict[RazorpayOrderStatus, frozenset[RazorpayOrderStatus]] = {
    RazorpayOrderStatus.CREATED: frozenset(
        {RazorpayOrderStatus.ATTEMPTED, RazorpayOrderStatus.PAID}
    ),
    RazorpayOrderStatus.ATTEMPTED: frozenset({RazorpayOrderStatus.PAID}),
    RazorpayOrderStatus.PAID: frozenset(),
}

_ATTEMPT_TRANSITIONS: dict[PaymentAttemptStatus, frozenset[PaymentAttemptStatus]] = {
    PaymentAttemptStatus.CREATED: frozenset(
        {
            PaymentAttemptStatus.AUTHORIZED,
            PaymentAttemptStatus.CAPTURED,
            PaymentAttemptStatus.FAILED,
            PaymentAttemptStatus.REFUNDED,
        }
    ),
    PaymentAttemptStatus.FAILED: frozenset(
        {
            PaymentAttemptStatus.AUTHORIZED,
            PaymentAttemptStatus.CAPTURED,
            PaymentAttemptStatus.REFUNDED,
        }
    ),
    PaymentAttemptStatus.AUTHORIZED: frozenset(
        {PaymentAttemptStatus.CAPTURED, PaymentAttemptStatus.REFUNDED}
    ),
    PaymentAttemptStatus.CAPTURED: frozenset({PaymentAttemptStatus.REFUNDED}),
    PaymentAttemptStatus.REFUNDED: frozenset(),
}


def can_transition_transaction(
    previous: PaymentTransactionState | str,
    next_state: PaymentTransactionState | str,
) -> bool:
    """Return whether a transaction observation is monotonic and legal."""
    previous_value = PaymentTransactionState(previous)
    next_value = PaymentTransactionState(next_state)
    return previous_value is next_value or next_value in _TRANSACTION_TRANSITIONS[previous_value]


def require_transaction_transition(
    previous: PaymentTransactionState | str,
    next_state: PaymentTransactionState | str,
) -> None:
    """Reject an illegal transaction transition with a stable exception type."""
    if not can_transition_transaction(previous, next_state):
        raise ValueError(
            f"Payment transaction transition {previous!s} -> {next_state!s} is not allowed"
        )


def can_transition_order_status(
    previous: RazorpayOrderStatus | str | None,
    next_status: RazorpayOrderStatus | str | None,
) -> bool:
    """Return whether newly observed Razorpay order state may replace the old state."""
    if previous is None:
        return next_status is None or RazorpayOrderStatus(next_status) in RazorpayOrderStatus
    if next_status is None:
        return False
    previous_value = RazorpayOrderStatus(previous)
    next_value = RazorpayOrderStatus(next_status)
    return previous_value is next_value or next_value in _ORDER_TRANSITIONS[previous_value]


def can_transition_attempt_status(
    previous: PaymentAttemptStatus | str,
    next_status: PaymentAttemptStatus | str,
) -> bool:
    """Return whether a provider payment observation may advance an attempt."""
    previous_value = PaymentAttemptStatus(previous)
    next_value = PaymentAttemptStatus(next_status)
    return previous_value is next_value or next_value in _ATTEMPT_TRANSITIONS[previous_value]
