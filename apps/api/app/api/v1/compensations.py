"""Authenticated buyer reads for compensation and refund evidence."""

from fastapi import APIRouter, HTTPException, Response

from app.api.v1.dependencies import (
    AuthenticatedMutationDependency,
    CompensationApplicationDependency,
    CurrentAccountDependency,
    OperatorRateLimiterDependency,
    RefundApplicationDependency,
    SettingsDependency,
)
from app.cache.operator import OperatorRateLimitExceeded
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
    limiter: OperatorRateLimiterDependency,
    settings: SettingsDependency,
) -> PaymentRefundResponse:
    try:
        await limiter.require(
            account_id=current.account.id,
            action="buyer-refund-reconcile",
            limit=settings.operator_reconciliation_rate_limit,
            window_seconds=settings.operator_rate_limit_window_seconds,
        )
    except OperatorRateLimitExceeded as error:
        raise HTTPException(
            429,
            detail={"code": "RECONCILIATION_RATE_LIMITED"},
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error
    except Exception as error:
        raise HTTPException(
            503, detail={"code": "RECONCILIATION_RATE_LIMIT_UNAVAILABLE"}
        ) from error
    refund = await application_service.reconcile_refund(
        refund_id,
        account_id=current.account.id,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return payment_refund_response(refund)
