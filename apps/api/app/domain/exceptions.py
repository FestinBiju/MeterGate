"""Safe domain failures translated at the HTTP boundary."""

from dataclasses import dataclass


class DomainError(Exception):
    """Base class for expected domain failures."""


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


class QuoteIntegrityError(DomainError):
    def __init__(self, quote_id: str) -> None:
        self.quote_id = quote_id
        super().__init__("Stored quote failed integrity verification")
