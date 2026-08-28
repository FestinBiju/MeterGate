"""Safe domain failures translated at the HTTP boundary."""

from dataclasses import dataclass


class DomainError(Exception):
    """Base class for expected domain failures."""


class McpSessionError(DomainError):
    """Fail-closed buyer-agent session or scope failure."""

    def __init__(self, detail: str, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


class McpSessionUnauthorizedError(McpSessionError):
    """A valid, active MCP bridge session is required."""


class McpSessionForbiddenError(McpSessionError):
    """The bridge session lacks scope or ownership."""


class McpSessionNotFoundError(McpSessionError):
    """An owned MCP session metadata record was not found."""


class McpSessionConflictError(McpSessionError):
    """The requested session operation conflicts with durable state."""


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


class CompensationError(DomainError):
    """Fail-closed compensation/refund failure with a stable public code."""

    def __init__(self, detail: str, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


class CompensationNotFoundError(CompensationError):
    """A compensation case or refund could not be found."""


class CompensationForbiddenError(CompensationError):
    """The authenticated account does not own this compensation evidence."""


class CompensationConflictError(CompensationError):
    """The requested compensation operation conflicts with durable state."""


class CompensationIntegrityError(CompensationError):
    """Stored or provider compensation evidence failed an integrity check."""


class CompensationUnavailableError(CompensationError):
    """The refund provider or durable recovery path is temporarily unavailable."""


class CompensationTimeoutError(CompensationError):
    """A bounded refund-provider operation timed out ambiguously."""


class EntitlementError(DomainError):
    """Fail-closed paid-access failure with a stable public code."""

    def __init__(self, detail: str, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


class EntitlementNotFoundError(EntitlementError):
    """An entitlement or its paid transaction could not be found."""


class EntitlementForbiddenError(EntitlementError):
    """The authenticated account does not own the entitlement."""


class EntitlementExpiredError(EntitlementError):
    """The short-lived paid entitlement is no longer active."""


class EntitlementConflictError(EntitlementError):
    """Payment or fulfillment state cannot currently release value."""


class EntitlementIntegrityError(EntitlementError):
    """Stored entitlement or parent commerce evidence failed verification."""


class EntitlementUnavailableError(EntitlementError):
    """A temporary provider or worker failure prevented value release."""


class CapabilityError(DomainError):
    """Signed protected-resource capability failure."""

    def __init__(self, detail: str, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


class CapabilityInvalidError(CapabilityError):
    """The bearer token is malformed, unsigned, or otherwise invalid."""


class CapabilityExpiredError(CapabilityError):
    """The bearer token lifetime has ended."""


class CapabilityForbiddenError(CapabilityError):
    """A valid capability does not bind the requested resource or input."""


class FulfillmentError(DomainError):
    """Protected merchant execution failure with a stable public code."""

    def __init__(self, detail: str, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


class FulfillmentConflictError(FulfillmentError):
    """The execution is owned, exhausted, or in a conflicting terminal state."""


class FulfillmentRetryableError(FulfillmentError):
    """The same logical execution may be retried with its capability."""


class FulfillmentPermanentError(FulfillmentError):
    """The execution failed permanently and requires compensation evidence."""


class FulfillmentIntegrityError(FulfillmentError):
    """Stored or merchant result evidence failed validation."""


class ResourceRequestError(DomainError):
    """A protected-resource request body is unsafe or malformed."""

    def __init__(self, detail: str, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


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
