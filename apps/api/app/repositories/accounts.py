"""Persistence access for authenticated buyer accounts."""

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AccountStatus, ApprovalIdentityStatus
from app.models import Account, ApprovalIdentity, PasskeyCredential


class AccountRepository:
    """Load accounts and atomically persist first-passkey signup state."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, account_id: str) -> Account | None:
        return await self._session.get(Account, account_id)

    async def get_for_update(self, account_id: str) -> Account | None:
        result = await self._session.scalars(
            select(Account)
            .where(Account.id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.one_or_none()

    async def create_signup_bundle(
        self,
        account: Account,
        *,
        identity: ApprovalIdentity,
        credential: PasskeyCredential,
    ) -> Account:
        """Create the active account and its first verified credential atomically."""
        if AccountStatus(account.status) is not AccountStatus.ACTIVE:
            raise ValueError("A verified signup account must be active")
        if ApprovalIdentityStatus(identity.status) is not ApprovalIdentityStatus.ACTIVE:
            raise ValueError("A verified signup approval identity must be active")
        if identity.account_id != account.id or identity.subject_ref != account.id:
            raise ValueError("Signup approval identity must be canonically account-bound")
        if credential.approval_identity_id != identity.id:
            raise ValueError("Signup credential must belong to the signup approval identity")

        for record in (account, identity, credential):
            bound_session = sqlalchemy_inspect(record).session
            if bound_session not in {None, self._session.sync_session}:
                raise ValueError("Signup record belongs to a different database session")

        self._session.add_all((account, identity, credential))
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        for record in (account, identity, credential):
            await self._session.refresh(record)
        return account
