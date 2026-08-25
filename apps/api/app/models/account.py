"""Authenticated buyer account and server-side session revocation state."""

from sqlalchemy import CheckConstraint, Enum, Index, Integer, String, event
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AccountStatus
from app.domain.ids import new_account_id
from app.models.base import Base, TimestampMixin


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "length(id) = 31 AND substr(id, 1, 5) = 'acct_'",
            name="id_length_prefix",
        ),
        CheckConstraint(
            "id ~ '^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 200 AND display_name = trim(display_name)",
            name="display_name_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name="status",
        ),
        CheckConstraint("session_version >= 1", name="session_version_positive"),
        CheckConstraint("updated_at >= created_at", name="updated_at_valid"),
        Index("ix_accounts_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(31), primary_key=True, default=new_account_id)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[AccountStatus] = mapped_column(
        Enum(
            AccountStatus,
            name="account_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=AccountStatus.PENDING,
        server_default=AccountStatus.PENDING.value,
    )
    session_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )


@event.listens_for(Account, "before_update")
def _guard_account_update(
    _mapper: object,
    _connection: object,
    account: Account,
) -> None:
    state = sqlalchemy_inspect(account)
    if any(state.attrs[field].history.has_changes() for field in ("id", "created_at")):
        raise InvalidRequestError("Account identity fields are immutable")

    version_history = state.attrs.session_version.history
    prior_version = (
        version_history.deleted[0]
        if version_history.has_changes() and version_history.deleted
        else account.session_version
    )
    if account.session_version < prior_version:
        raise InvalidRequestError("Account session version cannot regress")

    status_history = state.attrs.status.history
    if status_history.has_changes() and status_history.deleted:
        prior_status = AccountStatus(status_history.deleted[0])
        next_status = AccountStatus(account.status)
        allowed_transitions = {
            (AccountStatus.PENDING, AccountStatus.ACTIVE),
            (AccountStatus.PENDING, AccountStatus.DISABLED),
            (AccountStatus.ACTIVE, AccountStatus.DISABLED),
        }
        if (prior_status, next_status) not in allowed_transitions:
            raise InvalidRequestError("Account status transition is not allowed")
        if account.session_version <= prior_version:
            raise InvalidRequestError(
                "Account session version must advance when account status changes"
            )

    updated_history = state.attrs.updated_at.history
    if updated_history.has_changes() and updated_history.deleted:
        prior_updated_at = updated_history.deleted[0]
        if account.updated_at < prior_updated_at:
            raise InvalidRequestError("Account update time cannot regress")


@event.listens_for(Account, "before_delete")
def _reject_account_delete(*_: object) -> None:
    raise InvalidRequestError("Accounts cannot be deleted; disable them instead")
