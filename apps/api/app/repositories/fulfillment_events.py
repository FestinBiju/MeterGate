"""Read-only access to append-only fulfillment evidence."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FulfillmentEvent


class FulfillmentEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, event_id: str) -> FulfillmentEvent | None:
        return await self._session.get(FulfillmentEvent, event_id)

    async def get_by_idempotency_key(self, key: str) -> FulfillmentEvent | None:
        return await self._session.scalar(
            select(FulfillmentEvent).where(FulfillmentEvent.idempotency_key == key)
        )

    async def list_for_transaction(self, transaction_id: str) -> list[FulfillmentEvent]:
        records = await self._session.scalars(
            select(FulfillmentEvent)
            .where(FulfillmentEvent.transaction_id == transaction_id)
            .order_by(FulfillmentEvent.occurred_at, FulfillmentEvent.id)
        )
        return list(records)
