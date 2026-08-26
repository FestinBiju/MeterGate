"""Append-only compensation audit persistence."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompensationEvent


class CompensationEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(self, key: str) -> CompensationEvent | None:
        return await self._session.scalar(
            select(CompensationEvent).where(CompensationEvent.idempotency_key == key)
        )

    async def list_for_transaction(self, transaction_id: str) -> list[CompensationEvent]:
        records = await self._session.scalars(
            select(CompensationEvent)
            .where(CompensationEvent.transaction_id == transaction_id)
            .order_by(
                CompensationEvent.occurred_at,
                CompensationEvent.compensation_case_id,
                CompensationEvent.sequence,
                CompensationEvent.id,
            )
        )
        return list(records)

    async def list_for_case(self, case_id: str) -> list[CompensationEvent]:
        records = await self._session.scalars(
            select(CompensationEvent)
            .where(CompensationEvent.compensation_case_id == case_id)
            .order_by(CompensationEvent.sequence, CompensationEvent.id)
        )
        return list(records)

    def stage(self, event: CompensationEvent) -> None:
        """Stage audit evidence without committing the aggregate transaction."""
        self._session.add(event)
