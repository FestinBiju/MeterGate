"""Persistence access for registered WebAuthn credentials."""

from datetime import datetime

from sqlalchemy import inspect as sqlalchemy_inspect
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
            .execution_options(populate_existing=True)
        )
        return result.one_or_none()

    async def update_locked_usage(
        self,
        credential: PasskeyCredential,
        *,
        new_sign_count: int,
        last_used_at: datetime,
    ) -> PasskeyCredential:
        """Commit usage state for a credential already locked in this session."""
        if sqlalchemy_inspect(credential).session is not self._session.sync_session:
            raise ValueError("Locked credential belongs to a different database session")
        if type(new_sign_count) is not int or not 0 <= new_sign_count <= 4_294_967_295:
            raise ValueError("Passkey signature counter is outside the uint32 range")
        if new_sign_count < credential.sign_count:
            raise ValueError("Passkey signature counter cannot regress")
        if (
            not isinstance(last_used_at, datetime)
            or last_used_at.tzinfo is None
            or last_used_at.utcoffset() is None
        ):
            raise ValueError("Passkey last-used time must be timezone-aware")
        if credential.last_used_at is not None and last_used_at < credential.last_used_at:
            raise ValueError("Passkey last-used time cannot regress")

        credential.sign_count = new_sign_count
        credential.last_used_at = last_used_at
        self._session.add(credential)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(credential)
        return credential
