"""Lease-fenced operations for the durable refund outbox."""

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RefundOutboxEvent


class RefundOutboxEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, event_id: str) -> RefundOutboxEvent | None:
        return await self._session.get(RefundOutboxEvent, event_id)

    async def get_by_deduplication_key(self, key: str) -> RefundOutboxEvent | None:
        return await self._session.scalar(
            select(RefundOutboxEvent).where(RefundOutboxEvent.deduplication_key == key)
        )

    def stage(self, event: RefundOutboxEvent) -> None:
        """Stage work without committing so the compensation decision owns the transaction."""
        expected_key = f"refund:{event.compensation_case_id}"
        if event.deduplication_key != expected_key:
            raise ValueError("Refund outbox deduplication key does not bind its case")
        self._session.add(event)

    async def claim_next(
        self,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> RefundOutboxEvent | None:
        event = await self._session.scalar(
            select(RefundOutboxEvent)
            .where(
                RefundOutboxEvent.processed_at.is_(None),
                RefundOutboxEvent.available_at <= now,
                or_(
                    RefundOutboxEvent.lease_expires_at.is_(None),
                    RefundOutboxEvent.lease_expires_at <= now,
                ),
            )
            .order_by(RefundOutboxEvent.available_at, RefundOutboxEvent.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return None
        event.processing_started_at = now
        event.lease_expires_at = lease_expires_at
        event.lease_generation += 1
        event.attempt_count += 1
        event.last_error_code = None
        await self._session.commit()
        await self._session.refresh(event)
        return event

    async def release_for_retry(
        self,
        event: RefundOutboxEvent,
        *,
        expected_lease_generation: int,
        available_at: datetime,
        error_code: str,
    ) -> RefundOutboxEvent:
        locked = await self._session.scalar(
            select(RefundOutboxEvent)
            .where(RefundOutboxEvent.id == event.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            locked is None
            or locked.processed_at is not None
            or locked.lease_generation != expected_lease_generation
        ):
            await self._session.rollback()
            raise ValueError("Refund outbox lease is stale or already processed")
        locked.processing_started_at = None
        locked.lease_expires_at = None
        locked.available_at = available_at
        locked.last_error_code = error_code
        await self._session.commit()
        await self._session.refresh(locked)
        return locked

    async def mark_processed(
        self,
        event: RefundOutboxEvent,
        *,
        expected_lease_generation: int,
        processed_at: datetime,
    ) -> RefundOutboxEvent:
        locked = await self._session.scalar(
            select(RefundOutboxEvent)
            .where(RefundOutboxEvent.id == event.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            locked is None
            or locked.processed_at is not None
            or locked.lease_generation != expected_lease_generation
        ):
            await self._session.rollback()
            raise ValueError("Refund outbox lease is stale or already processed")
        locked.processed_at = processed_at
        locked.last_error_code = None
        await self._session.commit()
        await self._session.refresh(locked)
        return locked
