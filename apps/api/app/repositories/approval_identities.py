"""Persistence access for approval principals."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApprovalIdentity


class ApprovalIdentityRepository:
    """Create and load approval identities without exposing broad mutation methods."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, identity: ApprovalIdentity) -> ApprovalIdentity:
        self._session.add(identity)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(identity)
        return identity

    async def get(self, identity_id: str) -> ApprovalIdentity | None:
        return await self._session.get(ApprovalIdentity, identity_id)

    async def get_by_subject_ref(self, subject_ref: str) -> ApprovalIdentity | None:
        result = await self._session.scalars(
            select(ApprovalIdentity).where(ApprovalIdentity.subject_ref == subject_ref)
        )
        return result.one_or_none()

    async def get_for_update(self, identity_id: str) -> ApprovalIdentity | None:
        result = await self._session.scalars(
            select(ApprovalIdentity).where(ApprovalIdentity.id == identity_id).with_for_update()
        )
        return result.one_or_none()
