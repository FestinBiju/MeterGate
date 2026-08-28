"""Registered WebAuthn credential with narrowly mutable usage state."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.ids import new_passkey_credential_id
from app.models.base import Base

_NULLABLE_TRANSPORT_LIST = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True),
    "postgresql",
)


class PasskeyCredential(Base):
    __tablename__ = "passkey_credentials"
    __table_args__ = (
        UniqueConstraint("credential_id", name="uq_passkey_credentials_credential_id"),
        UniqueConstraint(
            "id",
            "approval_identity_id",
            name="uq_passkey_credentials_id_approval_identity_id",
        ),
        CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'pkc_'",
            name="id_length_prefix",
        ),
        CheckConstraint(
            "length(approval_identity_id) = 30 AND substr(approval_identity_id, 1, 4) = 'aid_'",
            name="approval_identity_id_length_prefix",
        ),
        CheckConstraint(
            "length(credential_id) BETWEEN 1 AND 1024",
            name="credential_id_length",
        ),
        CheckConstraint(
            "length(public_key) BETWEEN 1 AND 16384",
            name="public_key_length",
        ),
        CheckConstraint(
            "sign_count BETWEEN 0 AND 4294967295",
            name="sign_count_range",
        ),
        CheckConstraint(
            "id ~ '^pkc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "approval_identity_id ~ '^aid_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="approval_identity_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "CASE WHEN transports IS NULL THEN TRUE "
            "WHEN jsonb_typeof(transports) <> 'array' THEN FALSE "
            "ELSE jsonb_array_length(transports) BETWEEN 1 AND 16 "
            "AND NOT jsonb_path_exists(transports, "
            "'$[*] ? (@.type() != \"string\")') "
            "AND NOT jsonb_path_exists(transports, "
            "'$[*] ? (!(@ like_regex \"^[a-z][a-z0-9-]{0,31}$\"))') END",
            name="transports_valid",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_passkey_credentials_identity_created_at",
            "approval_identity_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
        default=new_passkey_credential_id,
    )
    approval_identity_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey(
            "approval_identities.id",
            name="fk_passkey_credentials_identity",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    credential_id: Mapped[bytes] = mapped_column(LargeBinary(1024), nullable=False)
    public_key: Mapped[bytes] = mapped_column(LargeBinary(16384), nullable=False)
    sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transports: Mapped[list[str] | None] = mapped_column(
        _NULLABLE_TRANSPORT_LIST,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


@event.listens_for(PasskeyCredential, "before_update")
def _guard_passkey_credential_update(
    _mapper: object,
    _connection: object,
    credential: PasskeyCredential,
) -> None:
    state = sqlalchemy_inspect(credential)
    immutable_fields = (
        "id",
        "approval_identity_id",
        "credential_id",
        "public_key",
        "transports",
        "created_at",
    )
    if any(state.attrs[field].history.has_changes() for field in immutable_fields):
        raise InvalidRequestError(
            "Passkey credential bindings are immutable; only usage state may change"
        )

    sign_count_history = state.attrs.sign_count.history
    if sign_count_history.has_changes() and sign_count_history.deleted:
        prior_sign_count = sign_count_history.deleted[0]
        if credential.sign_count < prior_sign_count:
            raise InvalidRequestError("Passkey signature counter cannot regress")

    last_used_history = state.attrs.last_used_at.history
    if last_used_history.has_changes() and last_used_history.deleted:
        prior_last_used_at = last_used_history.deleted[0]
        if prior_last_used_at is not None and (
            credential.last_used_at is None or credential.last_used_at < prior_last_used_at
        ):
            raise InvalidRequestError("Passkey last-used time cannot regress")


@event.listens_for(PasskeyCredential, "before_delete")
def _reject_passkey_credential_delete(*_: object) -> None:
    raise InvalidRequestError("Passkey credentials cannot be deleted")
