"""Pure transition rules for the fulfillment execution aggregate."""

from app.domain.enums import FulfillmentExecutionState

_TRANSITIONS: dict[FulfillmentExecutionState, frozenset[FulfillmentExecutionState]] = {
    FulfillmentExecutionState.PENDING: frozenset(
        {
            FulfillmentExecutionState.EXECUTING,
            FulfillmentExecutionState.PERMANENT_FAILURE,
            FulfillmentExecutionState.RECONCILIATION_REQUIRED,
        }
    ),
    FulfillmentExecutionState.EXECUTING: frozenset(
        {
            FulfillmentExecutionState.RETRYABLE_FAILURE,
            FulfillmentExecutionState.SUCCEEDED,
            FulfillmentExecutionState.PERMANENT_FAILURE,
            FulfillmentExecutionState.RECONCILIATION_REQUIRED,
        }
    ),
    FulfillmentExecutionState.RETRYABLE_FAILURE: frozenset(
        {
            FulfillmentExecutionState.EXECUTING,
            FulfillmentExecutionState.PERMANENT_FAILURE,
            FulfillmentExecutionState.RECONCILIATION_REQUIRED,
        }
    ),
    FulfillmentExecutionState.RECONCILIATION_REQUIRED: frozenset(
        {
            FulfillmentExecutionState.EXECUTING,
            FulfillmentExecutionState.SUCCEEDED,
            FulfillmentExecutionState.PERMANENT_FAILURE,
        }
    ),
    FulfillmentExecutionState.SUCCEEDED: frozenset(),
    FulfillmentExecutionState.PERMANENT_FAILURE: frozenset(),
}


def can_transition_fulfillment(
    prior: FulfillmentExecutionState,
    resulting: FulfillmentExecutionState,
) -> bool:
    """Return whether a persisted execution may move between the two states."""
    return resulting in _TRANSITIONS[prior]
