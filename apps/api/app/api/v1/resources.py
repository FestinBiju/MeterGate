"""Machine-readable HTTP 402 and capability-gated resource execution."""

import json
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.api.v1.dependencies import (
    PaymentProviderDependency,
    SessionDependency,
    SettingsDependency,
    _single_header,
)
from app.domain.enums import MerchantStatus, ServiceStatus
from app.domain.exceptions import (
    CapabilityInvalidError,
    FulfillmentPermanentError,
    QuoteInputIssue,
    QuoteInputValidationError,
    ResourceNotFoundError,
    ResourceRequestError,
    ServiceConfigurationError,
)
from app.domain.json_schema import MAX_INSTANCE_TEXT_BYTES
from app.domain.media_types import normalize_json_content_type
from app.domain.service_input import (
    ServiceInputConfigurationError,
    ServiceInputValueError,
    validate_normalize_and_hash_service_input,
)
from app.providers.fulfillment import FulfillmentProvider, UnavailableFulfillmentProvider
from app.repositories import MerchantRepository, ServiceRepository
from app.schemas.entitlements import (
    FulfillmentPendingResponse,
    FulfillmentResultResponse,
    PaymentRequiredResponse,
    QuoteLink,
    QuoteRequestDescription,
    ResourceAccessDescription,
    ResourceParty,
)
from app.services.capabilities import CapabilityTokenService
from app.services.fulfillments import FulfillmentApplicationService
from app.services.payments import PaymentApplicationService

router = APIRouter(prefix="/resources", tags=["protected resources"])


@router.post(
    "/{merchant_slug}/{service_slug}/execute",
    response_model=FulfillmentResultResponse | FulfillmentPendingResponse,
    responses={
        202: {"model": FulfillmentPendingResponse},
        402: {"model": PaymentRequiredResponse},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {}}},
        }
    },
)
async def execute_protected_resource(
    merchant_slug: str,
    service_slug: str,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    payment_provider: PaymentProviderDependency,
) -> FulfillmentResultResponse | FulfillmentPendingResponse | JSONResponse:
    authorization = _single_header(request, b"authorization")
    if authorization is not None and (
        not authorization.startswith("Bearer ")
        or len(authorization) <= len("Bearer ")
        or authorization != authorization.strip()
        or " " in authorization[len("Bearer ") :]
    ):
        raise CapabilityInvalidError(
            "A valid MeterGate Bearer capability is required",
            "CAPABILITY_INVALID",
        )
    payload = await _read_bounded_json(request)
    if authorization is None:
        challenge = await _payment_required(
            session,
            merchant_slug=merchant_slug,
            service_slug=service_slug,
            input_value=payload,
        )
        return JSONResponse(
            status_code=402,
            content=challenge.model_dump(mode="json"),
            headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
        )
    if not settings.fulfillment_enabled or settings.entitlement_token_secret is None:
        raise FulfillmentPermanentError(
            "Paid fulfillment is unavailable",
            "FULFILLMENT_PROVIDER_UNAVAILABLE",
        )
    provider = getattr(request.app.state, "fulfillment_provider", None)
    if provider is None or not isinstance(provider, FulfillmentProvider):
        provider = UnavailableFulfillmentProvider()
    capability_service = CapabilityTokenService(
        settings.entitlement_token_secret.get_secret_value(),
        ttl=timedelta(seconds=settings.entitlement_token_ttl_seconds),
    )
    application_service = FulfillmentApplicationService(
        session,
        capability_service,
        provider,
        payment_eligibility=PaymentApplicationService(session, payment_provider, settings),
        provider_base_url=settings.orbitintel_base_url,
        execution_lease=timedelta(seconds=settings.fulfillment_execution_lease_seconds),
        maximum_result_bytes=settings.fulfillment_max_result_bytes,
        default_maximum_attempts=settings.fulfillment_max_attempts,
    )
    operation = await application_service.execute(
        merchant_slug=merchant_slug,
        service_slug=service_slug,
        input_value=payload,
        token=authorization[len("Bearer ") :],
    )
    response.status_code = operation.status_code
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    if operation.result is None:
        return FulfillmentPendingResponse(
            execution_id=operation.execution_id,
            entitlement_id=operation.entitlement_id,
            reason_code=(
                "FULFILLMENT_PROVIDER_IN_PROGRESS"
                if operation.reason_code == "FULFILLMENT_PROVIDER_IN_PROGRESS"
                else "FULFILLMENT_ALREADY_CLAIMED"
            ),
        )
    result = operation.result
    return FulfillmentResultResponse(
        execution_id=result.execution_id,
        entitlement_id=result.entitlement_id,
        result_content_type=result.result_content_type,
        result=result.result,
        result_hash=result.result_hash,
        result_size_bytes=result.result_size_bytes,
        replayed_result=result.replayed_result,
        completed_at=result.completed_at,
    )


async def _read_bounded_json(request: Request) -> object:
    content_length = _single_header(request, b"content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise ResourceRequestError(
                "Protected resource Content-Length is invalid",
                "RESOURCE_REQUEST_BODY_INVALID",
            ) from error
        if declared_length < 0 or declared_length > MAX_INSTANCE_TEXT_BYTES:
            raise ResourceRequestError(
                "Protected resource body exceeds the configured limit",
                "RESOURCE_REQUEST_BODY_TOO_LARGE",
            )

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_INSTANCE_TEXT_BYTES:
            raise ResourceRequestError(
                "Protected resource body exceeds the configured limit",
                "RESOURCE_REQUEST_BODY_TOO_LARGE",
            )
    if not body:
        raise ResourceRequestError(
            "Protected resource body must contain one JSON value",
            "RESOURCE_REQUEST_BODY_INVALID",
        )

    def reject_nonstandard_number(value: str) -> object:
        raise ValueError(f"Non-standard JSON number {value!r}")

    try:
        return json.loads(
            bytes(body).decode("utf-8"),
            parse_constant=reject_nonstandard_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ResourceRequestError(
            "Protected resource body is not valid JSON",
            "RESOURCE_REQUEST_BODY_INVALID",
        ) from error


async def _payment_required(
    session: SessionDependency,
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
    party_merchant = ResourceParty(
        id=merchant.id,
        slug=merchant.slug,
        name=merchant.name,
    )
    party_service = ResourceParty(
        id=service.id,
        slug=service.slug,
        name=service.name,
    )
    return PaymentRequiredResponse(
        merchant=party_merchant,
        service=party_service,
        quote=QuoteLink(
            request=QuoteRequestDescription(
                service_id=service.id,
                input=canonical_input.value,
            )
        ),
        access=ResourceAccessDescription(),
    )
