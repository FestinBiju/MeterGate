"""Protected buyer payment routes and public signed Razorpay webhook ingress."""

from fastapi import APIRouter, Request, Response, status

from app.api.v1.dependencies import (
    AuthenticatedMutationDependency,
    CurrentAccountDependency,
    PaymentApplicationDependency,
    RazorpayWebhookIngressDependency,
    SettingsDependency,
    _single_header,
)
from app.domain.exceptions import PaymentVerificationError
from app.schemas.payments import (
    CheckoutVerificationCreate,
    PaymentTransactionCreate,
    PaymentTransactionResponse,
    RazorpayWebhookAccepted,
)

router = APIRouter(tags=["payments"])


@router.post(
    "/payment-transactions",
    response_model=PaymentTransactionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": PaymentTransactionResponse},
        202: {"model": PaymentTransactionResponse},
    },
)
async def create_payment_transaction(
    payload: PaymentTransactionCreate,
    response: Response,
    application_service: PaymentApplicationDependency,
    current: AuthenticatedMutationDependency,
) -> PaymentTransactionResponse:
    result = await application_service.create_transaction(
        payload,
        account_id=current.account.id,
    )
    response.status_code = result.status_code
    response.headers["Cache-Control"] = "private, no-store"
    return result.response


@router.get(
    "/payment-transactions/{transaction_id}",
    response_model=PaymentTransactionResponse,
)
async def get_payment_transaction(
    transaction_id: str,
    response: Response,
    application_service: PaymentApplicationDependency,
    current: CurrentAccountDependency,
) -> PaymentTransactionResponse:
    result = await application_service.get_transaction(
        transaction_id,
        account_id=current.account.id,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.post(
    "/payment-transactions/{transaction_id}/checkout/verify",
    response_model=PaymentTransactionResponse,
    responses={202: {"model": PaymentTransactionResponse}},
)
async def verify_payment_checkout(
    transaction_id: str,
    payload: CheckoutVerificationCreate,
    response: Response,
    application_service: PaymentApplicationDependency,
    current: AuthenticatedMutationDependency,
) -> PaymentTransactionResponse:
    result = await application_service.verify_checkout(
        transaction_id,
        payload,
        account_id=current.account.id,
    )
    response.status_code = result.status_code
    response.headers["Cache-Control"] = "private, no-store"
    return result.response


@router.post(
    "/payment-transactions/{transaction_id}/reconcile",
    response_model=PaymentTransactionResponse,
    responses={202: {"model": PaymentTransactionResponse}},
)
async def reconcile_payment_transaction(
    transaction_id: str,
    response: Response,
    application_service: PaymentApplicationDependency,
    current: AuthenticatedMutationDependency,
) -> PaymentTransactionResponse:
    result = await application_service.reconcile_transaction(
        transaction_id,
        account_id=current.account.id,
    )
    response.status_code = result.status_code
    response.headers["Cache-Control"] = "private, no-store"
    return result.response


@router.post(
    "/webhooks/razorpay",
    response_model=RazorpayWebhookAccepted,
)
async def accept_razorpay_webhook(
    request: Request,
    settings: SettingsDependency,
    ingress: RazorpayWebhookIngressDependency,
    application_service: PaymentApplicationDependency,
) -> RazorpayWebhookAccepted:
    raw_body = await _read_bounded_body(
        request,
        maximum_bytes=settings.razorpay_webhook_max_body_bytes,
    )
    await ingress.accept(
        raw_body=raw_body,
        signature=_single_header(request, b"x-razorpay-signature"),
        provider_event_id=_single_header(request, b"x-razorpay-event-id"),
        before_enqueue=application_service.quarantine_value_revoking_webhook,
    )
    return RazorpayWebhookAccepted(
        status="accepted",
        reason_code="PAYMENT_WEBHOOK_ACCEPTED",
    )


async def _read_bounded_body(request: Request, *, maximum_bytes: int) -> bytes:
    content_length = _single_header(request, b"content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise PaymentVerificationError(
                "Razorpay webhook Content-Length is invalid",
                "PAYMENT_WEBHOOK_BODY_INVALID",
            ) from error
        if declared_length < 0 or declared_length > maximum_bytes:
            raise PaymentVerificationError(
                "Razorpay webhook body exceeds the configured limit",
                "PAYMENT_WEBHOOK_BODY_TOO_LARGE",
            )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise PaymentVerificationError(
                "Razorpay webhook body exceeds the configured limit",
                "PAYMENT_WEBHOOK_BODY_TOO_LARGE",
            )
    if not body:
        raise PaymentVerificationError(
            "Razorpay webhook body is empty",
            "PAYMENT_WEBHOOK_BODY_INVALID",
        )
    return bytes(body)
