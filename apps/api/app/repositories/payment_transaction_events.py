"""Read access for append-only payment transaction audit events."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PaymentTransactionEvent


class PaymentTransactionEventRepository:
    """Expose audit lookups without independent mutation methods."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> PaymentTransactionEvent | None:
        result = await self._session.scalars(
            select(PaymentTransactionEvent).where(
                PaymentTransactionEvent.idempotency_key == idempotency_key
            )
        )
        return result.one_or_none()

    async def list_for_transaction(
        self,
        transaction_id: str,
    ) -> list[PaymentTransactionEvent]:
        result = await self._session.scalars(
            select(PaymentTransactionEvent)
            .where(PaymentTransactionEvent.transaction_id == transaction_id)
            .order_by(PaymentTransactionEvent.transaction_revision)
        )
        return list(result.all())
