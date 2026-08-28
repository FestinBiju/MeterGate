"""Read, lock, and stage durable refund reservations."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import PaymentRefundState
from app.models import PaymentRefund

_ACTIVE_REFUND_STATES = (
    PaymentRefundState.REFUND_PENDING,
    PaymentRefundState.REFUND_PROCESSING,
    PaymentRefundState.REFUND_UNCERTAIN,
    PaymentRefundState.RECONCILIATION_REQUIRED,
)


class PaymentRefundRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, refund_id: str) -> PaymentRefund | None:
        return await self._session.get(PaymentRefund, refund_id)

    async def get_for_update(self, refund_id: str) -> PaymentRefund | None:
        return await self._session.scalar(
            select(PaymentRefund)
            .where(PaymentRefund.id == refund_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def get_by_compensation_case_id(self, case_id: str) -> PaymentRefund | None:
        return await self._session.scalar(
            select(PaymentRefund)
            .where(PaymentRefund.compensation_case_id == case_id)
            .order_by(PaymentRefund.created_at.desc(), PaymentRefund.id.desc())
            .limit(1)
        )

    async def get_active_by_compensation_case_id(self, case_id: str) -> PaymentRefund | None:
        return await self._session.scalar(
            select(PaymentRefund).where(
                PaymentRefund.compensation_case_id == case_id,
                PaymentRefund.refund_state.in_(_ACTIVE_REFUND_STATES),
            )
        )

    async def get_by_provider_refund_id(self, provider_refund_id: str) -> PaymentRefund | None:
        return await self._session.scalar(
            select(PaymentRefund).where(PaymentRefund.provider_refund_id == provider_refund_id)
        )

    async def get_by_provider_receipt(self, provider_receipt: str) -> PaymentRefund | None:
        return await self._session.scalar(
            select(PaymentRefund).where(PaymentRefund.provider_receipt == provider_receipt)
        )

    async def list_for_transaction(self, transaction_id: str) -> list[PaymentRefund]:
        records = await self._session.scalars(
            select(PaymentRefund)
            .where(PaymentRefund.transaction_id == transaction_id)
            .order_by(PaymentRefund.created_at, PaymentRefund.id)
        )
        return list(records)

    async def reserved_amount_for_case(self, case_id: str) -> int:
        amount = await self._session.scalar(
            select(func.coalesce(func.sum(PaymentRefund.amount), 0)).where(
                PaymentRefund.compensation_case_id == case_id,
                PaymentRefund.refund_state != PaymentRefundState.REFUND_FAILED,
            )
        )
        return int(amount or 0)

    def stage(self, refund: PaymentRefund) -> None:
        """Stage a refund reservation; database triggers serialize amount limits."""
        if refund.provider_receipt != refund.id:
            raise ValueError("Refund provider receipt must equal the refund ID")
        self._session.add(refund)
