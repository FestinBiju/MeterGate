"""Safe domain failures translated at the HTTP boundary."""

from dataclasses import dataclass


class DomainError(Exception):
    """Base class for expected domain failures."""


class AuthenticationError(DomainError):
    """Fail-closed account/session failure with a stable public code."""

    def __init__(self, detail: str, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


class AuthenticationUnauthorizedError(AuthenticationError):
    """A valid active authenticated session is required."""


class AuthenticationForbiddenError(AuthenticationError):
    """An authenticated principal does not own or cannot perform an action."""


class AuthenticationNotFoundError(AuthenticationError):
    """An authentication challenge or account could not be found."""


class AuthenticationExpiredError(AuthenticationError):
    """A short-lived authentication ceremony expired."""


class AuthenticationConflictError(AuthenticationError):
    """Authentication state was already used or conflicts with durable state."""


class AuthenticationVerificationError(AuthenticationError):
    """A supplied passkey response could not be verified."""


class AuthenticationIntegrityError(AuthenticationError):
    """Persisted or ephemeral authentication state failed validation."""


class ApprovalError(DomainError):
    """Fail-closed trusted-approval failure with a stable public code."""

    def __init__(self, detail: str, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


class ApprovalNotFoundError(ApprovalError):
    """A trusted-approval resource or ephemeral challenge is unknown."""


class ApprovalExpiredError(ApprovalError):
    """A trusted-approval challenge or bound commerce record expired."""


class ApprovalVerificationError(ApprovalError):
    """A supplied WebAuthn ceremony response could not be verified."""


class ApprovalConflictError(ApprovalError):
    """Trusted approval is incompatible with current authoritative state."""


class ApprovalIntegrityError(ApprovalError):
    """Immutable approval evidence failed its integrity check."""


class PaymentError(DomainError):
    """Fail-closed payment-gate failure with a stable public code."""

    def __init__(self, detail: str, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


class PaymentNotFoundError(PaymentError):
    """A payment authorization or transaction could not be found."""


class PaymentExpiredError(PaymentError):
    """A new transaction cannot be claimed from an expired authorization."""


class PaymentVerificationError(PaymentError):
    """Untrusted checkout or webhook evidence failed verification."""


class PaymentConflictError(PaymentError):
    """Payment state conflicts with the requested operation."""


class PaymentIntegrityError(PaymentError):
    """Stored or provider payment evidence failed an integrity check."""


class PaymentProviderError(PaymentError):
    """Razorpay rejected a request or returned an invalid response."""


class PaymentUnavailableError(PaymentError):
    """Payment processing or its durable queue is unavailable."""


class PaymentTimeoutError(PaymentError):
    """A read-only provider operation exceeded its bounded timeout."""


class ResourceNotFoundError(DomainError):
    def __init__(self, resource: str, resource_id: str) -> None:
        self.resource = resource
        self.resource_id = resource_id
        super().__init__(f"{resource} '{resource_id}' was not found")


class SlugConflictError(DomainError):
    def __init__(self, resource: str, slug: str) -> None:
        self.resource = resource
        self.slug = slug
        super().__init__(f"{resource} slug '{slug}' already exists")


class InvalidStateTransitionError(DomainError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)


class QuoteConflictError(DomainError):
    """Base class for stable quote-issuance conflicts."""

    reason_code: str

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


class InactiveMerchantError(QuoteConflictError):
    reason_code = "MERCHANT_NOT_ACTIVE"

    def __init__(self, merchant_id: str) -> None:
        super().__init__(f"Merchant '{merchant_id}' is not active")


class InactiveServiceError(QuoteConflictError):
    reason_code = "SERVICE_NOT_ACTIVE"

    def __init__(self, service_id: str) -> None:
        super().__init__(f"Service '{service_id}' is not active")


class ServiceConfigurationError(QuoteConflictError):
    reason_code = "SERVICE_CONFIGURATION_INVALID"

    def __init__(self, service_id: str) -> None:
        super().__init__(f"Service '{service_id}' cannot currently issue quotes")


@dataclass(frozen=True, slots=True)
class QuoteInputIssue:
    path: tuple[str | int, ...]
    keyword: str
    error_type: str = "service_input_invalid"


class QuoteInputValidationError(DomainError):
    def __init__(self, issues: tuple[QuoteInputIssue, ...]) -> None:
        self.issues = issues
        super().__init__("Quote input is invalid")


class StoredIntegrityError(DomainError):
    """A persisted immutable record cannot cross the trust boundary."""

    def __init__(self, resource: str, resource_id: str, reason_code: str) -> None:
        self.resource = resource
        self.resource_id = resource_id
        self.reason_code = reason_code
        super().__init__(f"Stored {resource.lower()} failed integrity verification")


class QuoteIntegrityError(StoredIntegrityError):
    def __init__(
        self,
        quote_id: str,
        reason_code: str = "INTEGRITY_QUOTE_HASH_MISMATCH",
    ) -> None:
        super().__init__("Quote", quote_id, reason_code)


class PolicyIntegrityError(StoredIntegrityError):
    def __init__(
        self,
        policy_id: str,
        reason_code: str = "INTEGRITY_POLICY_HASH_MISMATCH",
    ) -> None:
        super().__init__("Policy", policy_id, reason_code)


class PolicyEvaluationIntegrityError(StoredIntegrityError):
    def __init__(self, evaluation_id: str) -> None:
        super().__init__(
            "Policy evaluation",
            evaluation_id,
            "INTEGRITY_POLICY_EVALUATION_DATA_INVALID",
        )


class PolicyTTLExceededError(DomainError):
    def __init__(self, maximum_seconds: int) -> None:
        self.maximum_seconds = maximum_seconds
        super().__init__("Requested policy lifetime exceeds the configured maximum")
