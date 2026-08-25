"""Persistence access for immutable policy evaluation evidence."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PolicyEvaluation


class PolicyEvaluationRepository:
    """Expose creation and lookup only; evaluations are append-only."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, evaluation: PolicyEvaluation) -> PolicyEvaluation:
        self._session.add(evaluation)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(evaluation)
        return evaluation

    async def get(self, evaluation_id: str) -> PolicyEvaluation | None:
        return await self._session.get(PolicyEvaluation, evaluation_id)
