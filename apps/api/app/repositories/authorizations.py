"""Atomic persistence for immutable purchase authorizations."""

from datetime import datetime

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PasskeyCredential, PurchaseAuthorization


class PurchaseAuthorizationRepository:
    """Persist authorization evidence together with credential usage state."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_with_locked_credential_update(
        self,
        authorization: PurchaseAuthorization,
        *,
        credential: PasskeyCredential,
        new_sign_count: int,
        last_used_at: datetime,
    ) -> PurchaseAuthorization:
        """Commit the counter update and authorization insertion atomically.

        The caller must load ``credential`` using the row-locking repository method
        on this same request-scoped AsyncSession before verifying the assertion.
        """
        if sqlalchemy_inspect(credential).session is not self._session.sync_session:
            raise ValueError("Locked credential belongs to a different database session")
        if authorization.passkey_credential_id != credential.id:
            raise ValueError("Authorization does not reference the locked credential")
        if authorization.approval_identity_id != credential.approval_identity_id:
            raise ValueError("Authorization identity does not own the locked credential")
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
        self._session.add(authorization)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(credential)
        await self._session.refresh(authorization)
        return authorization

    async def get(self, authorization_id: str) -> PurchaseAuthorization | None:
        return await self._session.get(PurchaseAuthorization, authorization_id)

    async def get_for_update(
        self,
        authorization_id: str,
    ) -> PurchaseAuthorization | None:
        result = await self._session.scalars(
            select(PurchaseAuthorization)
            .where(PurchaseAuthorization.id == authorization_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.one_or_none()
