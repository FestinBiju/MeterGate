"""Private server-side routing configuration for merchant fulfillment."""

from urllib.parse import urlsplit

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import FulfillmentProviderType
from app.domain.ids import new_service_fulfillment_config_id
from app.models.base import Base, TimestampMixin


class ServiceFulfillmentConfig(TimestampMixin, Base):
    """Non-catalog merchant endpoint settings kept behind the MeterGate API."""

    __tablename__ = "service_fulfillment_configs"
    __table_args__ = (
        UniqueConstraint("service_id", name="uq_service_fulfillment_configs_service_id"),
        CheckConstraint("length(id) = 30 AND substr(id, 1, 4) = 'sfc_'", name="id_prefix"),
        CheckConstraint("provider_type = 'http'", name="provider_type"),
        CheckConstraint(
            "length(endpoint_url) BETWEEN 1 AND 2048 AND endpoint_url = trim(endpoint_url)",
            name="endpoint_url_valid",
        ),
        CheckConstraint(
            "request_timeout_seconds BETWEEN 1 AND 60", name="request_timeout_seconds_range"
        ),
        CheckConstraint("maximum_attempts BETWEEN 1 AND 10", name="maximum_attempts_range"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("updated_at >= created_at", name="updated_at_valid"),
        CheckConstraint("id ~ '^sfc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="id_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            "endpoint_url ~ '^https?://[^[:space:]]+$'", name="endpoint_url_format"
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[str] = mapped_column(
        String(30), primary_key=True, default=new_service_fulfillment_config_id
    )
    service_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    provider_type: Mapped[FulfillmentProviderType] = mapped_column(
        Enum(
            FulfillmentProviderType,
            name="fulfillment_provider_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=FulfillmentProviderType.HTTP,
        server_default=FulfillmentProviderType.HTTP.value,
    )
    endpoint_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    request_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )
    maximum_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")


def _validate_private_endpoint(config: ServiceFulfillmentConfig) -> None:
    parsed = urlsplit(config.endpoint_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidRequestError(
            "Fulfillment endpoint must be an HTTP(S) URL without credentials, query, or fragment"
        )


@event.listens_for(ServiceFulfillmentConfig, "before_insert")
def _validate_service_fulfillment_config_insert(
    _mapper: object,
    _connection: object,
    config: ServiceFulfillmentConfig,
) -> None:
    _validate_private_endpoint(config)


@event.listens_for(ServiceFulfillmentConfig, "before_update")
def _guard_service_fulfillment_config_update(
    _mapper: object,
    _connection: object,
    config: ServiceFulfillmentConfig,
) -> None:
    state = sqlalchemy_inspect(config)
    if any(
        state.attrs[field].history.has_changes() for field in ("id", "service_id", "created_at")
    ):
        raise InvalidRequestError("Service fulfillment config identity is immutable")
    revision_history = state.attrs.revision.history
    if not revision_history.has_changes() or not revision_history.deleted:
        raise InvalidRequestError("Service fulfillment config revision must advance exactly once")
    if config.revision != revision_history.deleted[0] + 1:
        raise InvalidRequestError("Service fulfillment config revision must advance exactly once")
    _validate_private_endpoint(config)


@event.listens_for(ServiceFulfillmentConfig, "before_delete")
def _reject_service_fulfillment_config_delete(*_: object) -> None:
    raise InvalidRequestError("Disable service fulfillment config instead of deleting it")
