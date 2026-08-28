"""Read, lock, and transaction-owned staging for compensation decisions."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompensationCase, CompensationEvent, RefundOutboxEvent


class CompensationCaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, case_id: str) -> CompensationCase | None:
        return await self._session.get(CompensationCase, case_id)

    async def get_for_update(self, case_id: str) -> CompensationCase | None:
        return await self._session.scalar(
            select(CompensationCase)
            .where(CompensationCase.id == case_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def get_by_transaction_id(self, transaction_id: str) -> CompensationCase | None:
        return await self._session.scalar(
            select(CompensationCase)
            .where(CompensationCase.transaction_id == transaction_id)
            .order_by(CompensationCase.created_at, CompensationCase.id)
            .limit(1)
        )

    async def get_by_execution_id(self, execution_id: str) -> CompensationCase | None:
        return await self._session.scalar(
            select(CompensationCase).where(
                CompensationCase.fulfillment_execution_id == execution_id
            )
        )

    def stage(
        self,
        case: CompensationCase,
        *,
        events: Sequence[CompensationEvent] = (),
        refund_outbox_event: RefundOutboxEvent | None = None,
    ) -> None:
        """Stage the complete decision evidence; the surrounding unit owns commit."""
        for event in events:
            if event.compensation_case_id != case.id or event.transaction_id != case.transaction_id:
                raise ValueError("Compensation event references a different case")
        if refund_outbox_event is not None:
            if refund_outbox_event.compensation_case_id != case.id:
                raise ValueError("Refund outbox event references a different case")
            if refund_outbox_event.deduplication_key != f"refund:{case.id}":
                raise ValueError("Refund outbox deduplication key does not bind its case")
        self._session.add(case)
        self._session.add_all(events)
        if refund_outbox_event is not None:
            self._session.add(refund_outbox_event)
