"""Persistence access for registered WebAuthn credentials."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PasskeyCredential


class PasskeyCredentialRepository:
    """Keep credential lookup and row locking out of WebAuthn orchestration."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, credential: PasskeyCredential) -> PasskeyCredential:
        self._session.add(credential)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(credential)
        return credential

    async def get(self, credential_id: str) -> PasskeyCredential | None:
        return await self._session.get(PasskeyCredential, credential_id)

    async def get_by_credential_id(self, credential_id: bytes) -> PasskeyCredential | None:
        result = await self._session.scalars(
            select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id)
        )
        return result.one_or_none()

    async def list_for_identity(self, identity_id: str) -> list[PasskeyCredential]:
        result = await self._session.scalars(
            select(PasskeyCredential)
            .where(PasskeyCredential.approval_identity_id == identity_id)
            .order_by(PasskeyCredential.created_at.asc(), PasskeyCredential.id.asc())
        )
        return list(result.all())

    async def get_by_credential_id_for_update(
        self,
        credential_id: bytes,
        *,
        identity_id: str,
    ) -> PasskeyCredential | None:
        """Lock the exact identity-bound credential through authorization commit."""
        result = await self._session.scalars(
            select(PasskeyCredential)
            .where(
                PasskeyCredential.credential_id == credential_id,
                PasskeyCredential.approval_identity_id == identity_id,
            )
            .with_for_update()
        )
        return result.one_or_none()
