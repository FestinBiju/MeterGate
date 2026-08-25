"""Approval principal bound to one MeterGate policy subject."""

import secrets

from sqlalchemy import CheckConstraint, Enum, Index, LargeBinary, String, UniqueConstraint, event
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import ApprovalIdentityStatus
from app.domain.ids import new_approval_identity_id
from app.models.base import Base, TimestampMixin


def _new_webauthn_user_handle() -> bytes:
    """Generate the private, opaque WebAuthn user handle for one identity."""
    return secrets.token_bytes(32)


class ApprovalIdentity(TimestampMixin, Base):
    __tablename__ = "approval_identities"
    __table_args__ = (
        UniqueConstraint("subject_ref", name="uq_approval_identities_subject_ref"),
        UniqueConstraint(
            "webauthn_user_handle",
            name="uq_approval_identities_webauthn_user_handle",
        ),
        CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'aid_'",
            name="id_length_prefix",
        ),
        CheckConstraint(
            "length(subject_ref) BETWEEN 1 AND 200 AND subject_ref = trim(subject_ref)",
            name="subject_ref_valid",
        ),
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 200 AND display_name = trim(display_name)",
            name="display_name_valid",
        ),
        CheckConstraint(
            "length(webauthn_user_handle) = 32",
            name="webauthn_user_handle_length",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="status",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="updated_at_valid",
        ),
        CheckConstraint(
            "id ~ '^aid_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="id_format",
        ).ddl_if(dialect="postgresql"),
        Index("ix_approval_identities_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
        default=new_approval_identity_id,
    )
    subject_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    webauthn_user_handle: Mapped[bytes] = mapped_column(
        LargeBinary(32),
        nullable=False,
        default=_new_webauthn_user_handle,
    )
    status: Mapped[ApprovalIdentityStatus] = mapped_column(
        Enum(
            ApprovalIdentityStatus,
            name="approval_identity_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=ApprovalIdentityStatus.ACTIVE,
        server_default=ApprovalIdentityStatus.ACTIVE.value,
    )


@event.listens_for(ApprovalIdentity, "before_update")
def _guard_approval_identity_update(
    _mapper: object,
    _connection: object,
    identity: ApprovalIdentity,
) -> None:
    state = sqlalchemy_inspect(identity)
    immutable_fields = ("id", "subject_ref", "webauthn_user_handle", "created_at")
    if any(state.attrs[field].history.has_changes() for field in immutable_fields):
        raise InvalidRequestError(
            "Approval identity bindings are immutable; only display name and status may change"
        )


@event.listens_for(ApprovalIdentity, "before_delete")
def _reject_approval_identity_delete(*_: object) -> None:
    raise InvalidRequestError("Approval identities cannot be deleted; disable them instead")
