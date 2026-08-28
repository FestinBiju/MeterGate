"""Read and lock access for provider payment attempts."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PaymentAttempt


class PaymentAttemptRepository:
    """Load attempts; aggregate writes are owned by PaymentTransactionRepository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, attempt_id: str) -> PaymentAttempt | None:
        return await self._session.get(PaymentAttempt, attempt_id)

    async def get_by_provider_payment_id(
        self,
        provider_payment_id: str,
    ) -> PaymentAttempt | None:
        result = await self._session.scalars(
            select(PaymentAttempt).where(PaymentAttempt.provider_payment_id == provider_payment_id)
        )
        return result.one_or_none()

    async def get_by_provider_payment_id_for_update(
        self,
        provider_payment_id: str,
    ) -> PaymentAttempt | None:
        result = await self._session.scalars(
            select(PaymentAttempt)
            .where(PaymentAttempt.provider_payment_id == provider_payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.one_or_none()

    async def list_for_transaction(self, transaction_id: str) -> list[PaymentAttempt]:
        result = await self._session.scalars(
            select(PaymentAttempt)
            .where(PaymentAttempt.transaction_id == transaction_id)
            .order_by(PaymentAttempt.first_seen_at, PaymentAttempt.id)
        )
        return list(result.all())
