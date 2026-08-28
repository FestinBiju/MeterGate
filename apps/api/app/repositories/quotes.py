"""Persistence access for append-only quote records."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Quote


class QuoteRepository:
    """Expose creation and lookup only; quotes have no mutation operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, quote: Quote) -> Quote:
        self._session.add(quote)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(quote)
        return quote

    async def get(self, quote_id: str) -> Quote | None:
        return await self._session.get(Quote, quote_id)
