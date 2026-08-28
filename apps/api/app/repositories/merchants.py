"""Async persistence operations for merchants."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Merchant


class MerchantRepository:
    """Keep merchant queries and persistence out of HTTP handlers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, merchant: Merchant) -> Merchant:
        self._session.add(merchant)
        await self._commit_and_refresh(merchant)
        return merchant

    async def get(self, merchant_id: str) -> Merchant | None:
        return await self._session.get(Merchant, merchant_id)

    async def get_by_slug(self, slug: str) -> Merchant | None:
        result = await self._session.scalars(select(Merchant).where(Merchant.slug == slug))
        return result.one_or_none()

    async def list(self) -> list[Merchant]:
        result = await self._session.scalars(
            select(Merchant).order_by(Merchant.slug.asc(), Merchant.id.asc())
        )
        return list(result.all())

    async def update(self, merchant: Merchant) -> Merchant:
        await self._commit_and_refresh(merchant)
        return merchant

    async def _commit_and_refresh(self, merchant: Merchant) -> None:
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(merchant)
