"""Atomic state revision and audit persistence for fulfillment executions."""

from datetime import datetime

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import FulfillmentExecutionState
from app.models import Entitlement, FulfillmentEvent, FulfillmentExecution


class FulfillmentExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, execution_id: str) -> FulfillmentExecution | None:
        return await self._session.get(FulfillmentExecution, execution_id)

    async def get_by_entitlement_id(self, entitlement_id: str) -> FulfillmentExecution | None:
        return await self._session.scalar(
            select(FulfillmentExecution).where(
                FulfillmentExecution.entitlement_id == entitlement_id
            )
        )

    async def get_by_entitlement_id_for_update(
        self, entitlement_id: str
    ) -> FulfillmentExecution | None:
        return await self._session.scalar(
            select(FulfillmentExecution)
            .where(FulfillmentExecution.entitlement_id == entitlement_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def claim_expired_recoverable_for_update(
        self,
        *,
        now: datetime,
    ) -> FulfillmentExecution | None:
        """Lock one expired, nonterminal execution whose active lease cannot still win."""
        return await self._session.scalar(
            select(FulfillmentExecution)
            .join(Entitlement, Entitlement.id == FulfillmentExecution.entitlement_id)
            .where(
                Entitlement.expires_at <= now,
                FulfillmentExecution.execution_state.in_(
                    (
                        FulfillmentExecutionState.PENDING,
                        FulfillmentExecutionState.EXECUTING,
                        FulfillmentExecutionState.RETRYABLE_FAILURE,
                    )
                ),
                or_(
                    FulfillmentExecution.execution_state != FulfillmentExecutionState.EXECUTING,
                    FulfillmentExecution.lease_expires_at <= now,
                ),
            )
            .order_by(Entitlement.expires_at, FulfillmentExecution.id)
            .limit(1)
            .with_for_update(of=FulfillmentExecution, skip_locked=True)
            .execution_options(populate_existing=True)
        )

    async def create_with_event(
        self,
        execution: FulfillmentExecution,
        *,
        event: FulfillmentEvent,
    ) -> FulfillmentExecution:
        self._validate_event(execution, event, expected_revision=1)
        if execution.revision != 1:
            raise ValueError("A new fulfillment execution must start at revision 1")
        self._session.add(execution)
        try:
            await self._session.flush()
            self._session.add(event)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(execution)
        await self._session.refresh(event)
        return execution

    async def update_with_event(
        self,
        execution: FulfillmentExecution,
        *,
        event: FulfillmentEvent,
    ) -> FulfillmentExecution:
        if sqlalchemy_inspect(execution).session is not self._session.sync_session:
            raise ValueError("Fulfillment execution must be loaded on this database session")
        next_revision = execution.revision + 1
        self._validate_event(execution, event, expected_revision=next_revision)
        execution.revision = next_revision
        self._session.add(event)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(execution)
        await self._session.refresh(event)
        return execution

    @staticmethod
    def _validate_event(
        execution: FulfillmentExecution,
        event: FulfillmentEvent,
        *,
        expected_revision: int,
    ) -> None:
        if event.execution_id != execution.id or event.entitlement_id != execution.entitlement_id:
            raise ValueError("Fulfillment event references a different execution")
        if event.transaction_id != execution.transaction_id:
            raise ValueError("Fulfillment event references a different transaction")
        if event.execution_revision != expected_revision:
            raise ValueError("Fulfillment event revision does not match the aggregate revision")
