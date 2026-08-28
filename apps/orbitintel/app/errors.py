"""Sanitized stable failures at OrbitIntel's private HTTP boundary."""


class OrbitIntelError(RuntimeError):
    """Base expected failure with a stable machine-readable reason code."""

    def __init__(self, message: str, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class InternalAuthenticationError(OrbitIntelError):
    pass


class RequestBodyError(OrbitIntelError):
    """A bounded private-request body could not be accepted safely."""


class InputBindingError(OrbitIntelError):
    pass


class UnsupportedServiceError(OrbitIntelError):
    pass


class CelesTrakNotFoundError(OrbitIntelError):
    pass


class CelesTrakUnavailableError(OrbitIntelError):
    pass


class CelesTrakResponseError(OrbitIntelError):
    pass


class CacheUnavailableError(OrbitIntelError):
    pass


class IdempotencyConflictError(OrbitIntelError):
    pass


class IdempotencyInProgressError(OrbitIntelError):
    pass


class IdempotencyUncertainError(OrbitIntelError):
    pass


class IdempotencyIntegrityError(OrbitIntelError):
    pass


class FaultInjectedError(OrbitIntelError):
    def __init__(self, message: str, reason_code: str, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__(message, reason_code)
