"""Persistence access for idempotent append-only Razorpay webhook evidence."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RazorpayWebhookEvent


class RazorpayWebhookEventRepository:
    """Persist terminal unassociated outcomes and look up provider event IDs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, event: RazorpayWebhookEvent) -> RazorpayWebhookEvent:
        self._session.add(event)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(event)
        return event

    async def get(self, event_id: str) -> RazorpayWebhookEvent | None:
        return await self._session.get(RazorpayWebhookEvent, event_id)

    async def get_by_provider_event_id(
        self,
        provider_event_id: str,
    ) -> RazorpayWebhookEvent | None:
        result = await self._session.scalars(
            select(RazorpayWebhookEvent).where(
                RazorpayWebhookEvent.provider_event_id == provider_event_id
            )
        )
        return result.one_or_none()
