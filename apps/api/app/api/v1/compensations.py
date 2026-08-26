"""Authenticated buyer reads for compensation and refund evidence."""

from fastapi import APIRouter, Response

from app.api.v1.dependencies import (
    AuthenticatedMutationDependency,
    CompensationApplicationDependency,
    CurrentAccountDependency,
    RefundApplicationDependency,
)
from app.schemas.compensations import (
    CompensationCaseResponse,
    CompensationId,
    PaymentRefundId,
    PaymentRefundResponse,
    PaymentTransactionId,
)
from app.services.compensations import payment_refund_response

router = APIRouter(tags=["compensations"])


@router.get(
    "/payment-transactions/{transaction_id}/compensation",
    response_model=CompensationCaseResponse,
)
async def get_transaction_compensation(
    transaction_id: PaymentTransactionId,
    response: Response,
    application_service: CompensationApplicationDependency,
    current: CurrentAccountDependency,
) -> CompensationCaseResponse:
    result = await application_service.get_for_transaction(
        transaction_id,
        account_id=current.account.id,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.get(
    "/compensations/{compensation_id}",
    response_model=CompensationCaseResponse,
)
async def get_compensation_case(
    compensation_id: CompensationId,
    response: Response,
    application_service: CompensationApplicationDependency,
    current: CurrentAccountDependency,
) -> CompensationCaseResponse:
    result = await application_service.get_case(
        compensation_id,
        account_id=current.account.id,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.get(
    "/refunds/{refund_id}",
    response_model=PaymentRefundResponse,
)
async def get_payment_refund(
    refund_id: PaymentRefundId,
    response: Response,
    application_service: CompensationApplicationDependency,
    current: CurrentAccountDependency,
) -> PaymentRefundResponse:
    result = await application_service.get_refund(
        refund_id,
        account_id=current.account.id,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.post(
    "/refunds/{refund_id}/reconcile",
    response_model=PaymentRefundResponse,
)
async def reconcile_payment_refund(
    refund_id: PaymentRefundId,
    response: Response,
    application_service: RefundApplicationDependency,
    current: AuthenticatedMutationDependency,
) -> PaymentRefundResponse:
    refund = await application_service.reconcile_refund(
        refund_id,
        account_id=current.account.id,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return payment_refund_response(refund)
