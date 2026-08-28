"""Shared protected-resource requirement projection used by HTTP and MCP."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MerchantStatus, ServiceStatus
from app.domain.exceptions import (
    QuoteInputIssue,
    QuoteInputValidationError,
    ResourceNotFoundError,
    ServiceConfigurationError,
)
from app.domain.media_types import normalize_json_content_type
from app.domain.service_input import (
    ServiceInputConfigurationError,
    ServiceInputValueError,
    validate_normalize_and_hash_service_input,
)
from app.repositories import MerchantRepository, ServiceRepository
from app.schemas.entitlements import (
    PaymentRequiredResponse,
    QuoteLink,
    QuoteRequestDescription,
    ResourceParty,
)


async def payment_required_projection(
    session: AsyncSession,
    *,
    merchant_slug: str,
    service_slug: str,
    input_value: object,
) -> PaymentRequiredResponse:
    merchant = await MerchantRepository(session).get_by_slug(merchant_slug)
    if merchant is None or MerchantStatus(merchant.status) is not MerchantStatus.ACTIVE:
        raise ResourceNotFoundError("Merchant", merchant_slug)
    service = await ServiceRepository(session).get_by_slug(merchant.id, service_slug)
    if service is None or ServiceStatus(service.status) is not ServiceStatus.ACTIVE:
        raise ResourceNotFoundError("Service", service_slug)
    if normalize_json_content_type(service.output_content_type) is None:
        raise ServiceConfigurationError(service.id)
    try:
        canonical_input = validate_normalize_and_hash_service_input(
            input_value,
            service.input_schema,
        )
    except ServiceInputValueError as error:
        raise QuoteInputValidationError(
            tuple(QuoteInputIssue(path=path, keyword=keyword) for path, keyword in error.violations)
        ) from error
    except ServiceInputConfigurationError as error:
        raise ServiceConfigurationError(service.id) from error
    return PaymentRequiredResponse(
        merchant=ResourceParty(id=merchant.id, slug=merchant.slug, name=merchant.name),
        service=ResourceParty(id=service.id, slug=service.slug, name=service.name),
        quote=QuoteLink(
            request=QuoteRequestDescription(
                service_id=service.id,
                input=canonical_input.value,
            )
        ),
    )
