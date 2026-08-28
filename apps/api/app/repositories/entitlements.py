"""Persistence operations for immutable entitlements."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import FulfillmentEventType
from app.models import Entitlement, FulfillmentEvent


class EntitlementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, entitlement_id: str) -> Entitlement | None:
        return await self._session.get(Entitlement, entitlement_id)

    async def get_by_transaction_id(self, transaction_id: str) -> Entitlement | None:
        return await self._session.scalar(
            select(Entitlement).where(Entitlement.transaction_id == transaction_id)
        )

    async def create_with_event(
        self,
        entitlement: Entitlement,
        *,
        event: FulfillmentEvent,
    ) -> Entitlement:
        if event.entitlement_id != entitlement.id:
            raise ValueError("Entitlement event references a different entitlement")
        if event.transaction_id != entitlement.transaction_id:
            raise ValueError("Entitlement event references a different transaction")
        if event.event_type is not FulfillmentEventType.ENTITLEMENT_ISSUED:
            raise ValueError("Entitlement creation requires entitlement-issued evidence")
        self._session.add(entitlement)
        try:
            await self._session.flush()
            self._session.add(event)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(entitlement)
        await self._session.refresh(event)
        return entitlement
