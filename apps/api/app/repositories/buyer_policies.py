"""Persistence access for immutable buyer policy records."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BuyerPolicy


class BuyerPolicyRepository:
    """Expose creation and lookup only; policies have no mutation operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, policy: BuyerPolicy) -> BuyerPolicy:
        self._session.add(policy)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(policy)
        return policy

    async def get(self, policy_id: str) -> BuyerPolicy | None:
        return await self._session.get(BuyerPolicy, policy_id)
