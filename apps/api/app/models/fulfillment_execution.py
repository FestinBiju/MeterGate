"""Stateful, retry-safe execution of one immutable entitlement."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import FulfillmentExecutionState, FulfillmentProviderType
from app.domain.fulfillment_state import can_transition_fulfillment
from app.domain.ids import new_fulfillment_execution_id
from app.models.base import Base, TimestampMixin

MAX_FULFILLMENT_RESULT_BYTES = 1_048_576


class FulfillmentExecution(TimestampMixin, Base):
    """One merchant-side effect and its durable retrievable result."""

    __tablename__ = "fulfillment_executions"
    __table_args__ = (
        UniqueConstraint("entitlement_id", name="uq_fulfillment_executions_entitlement_id"),
        CheckConstraint("length(id) = 30 AND substr(id, 1, 4) = 'ful_'", name="id_prefix"),
        CheckConstraint(
            "length(entitlement_id) = 30 AND substr(entitlement_id, 1, 4) = 'ent_'",
            name="entitlement_id_prefix",
        ),
        CheckConstraint(
            "length(transaction_id) = 30 AND substr(transaction_id, 1, 4) = 'txn_'",
            name="transaction_id_prefix",
        ),
        CheckConstraint(
            "execution_state IN ('pending', 'executing', 'retryable_failure', 'succeeded', "
            "'permanent_failure', 'reconciliation_required')",
            name="execution_state",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("lease_generation >= 0", name="lease_generation_nonnegative"),
        CheckConstraint(
            "((fulfillment_config_id IS NULL AND fulfillment_config_revision IS NULL "
            "AND provider_type IS NULL AND endpoint_url IS NULL "
            "AND request_timeout_seconds IS NULL AND maximum_attempts IS NULL) OR "
            "(fulfillment_config_id IS NOT NULL AND fulfillment_config_revision IS NOT NULL "
            "AND provider_type IS NOT NULL AND endpoint_url IS NOT NULL "
            "AND request_timeout_seconds IS NOT NULL AND maximum_attempts IS NOT NULL))",
            name="route_snapshot_complete",
        ),
        CheckConstraint(
            "fulfillment_config_revision IS NULL OR fulfillment_config_revision >= 1",
            name="fulfillment_config_revision_positive",
        ),
        CheckConstraint(
            "provider_type IS NULL OR provider_type = 'http'",
            name="provider_type",
        ),
        CheckConstraint(
            "endpoint_url IS NULL OR (length(endpoint_url) BETWEEN 1 AND 2048 "
            "AND endpoint_url = trim(endpoint_url))",
            name="endpoint_url_valid",
        ),
        CheckConstraint(
            "request_timeout_seconds IS NULL OR request_timeout_seconds BETWEEN 1 AND 60",
            name="request_timeout_seconds_range",
        ),
        CheckConstraint(
            "maximum_attempts IS NULL OR maximum_attempts BETWEEN 1 AND 10",
            name="maximum_attempts_range",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > started_at", name="lease_after_start"
        ),
        CheckConstraint(
            "result_size_bytes IS NULL OR "
            f"result_size_bytes BETWEEN 0 AND {MAX_FULFILLMENT_RESULT_BYTES}",
            name="result_size_bounded",
        ),
        CheckConstraint(
            "(result_content_type IS NULL AND result_json IS NULL AND result_hash IS NULL "
            "AND result_size_bytes IS NULL) OR "
            "(result_content_type IS NOT NULL AND result_json IS NOT NULL "
            "AND result_hash IS NOT NULL AND result_size_bytes IS NOT NULL)",
            name="result_complete",
        ),
        CheckConstraint(
            "failure_code IS NULL OR (length(failure_code) BETWEEN 1 AND 100 "
            "AND failure_code = trim(failure_code))",
            name="failure_code_valid",
        ),
        CheckConstraint("updated_at >= created_at", name="updated_at_valid"),
        CheckConstraint(
            "((execution_state = 'pending' AND attempt_count = 0 AND started_at IS NULL "
            "AND completed_at IS NULL AND failed_at IS NULL AND result_hash IS NULL "
            "AND failure_code IS NULL AND NOT compensation_required AND lease_expires_at IS NULL) "
            "OR (execution_state = 'executing' AND attempt_count >= 1 AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND failed_at IS NULL AND result_hash IS NULL "
            "AND failure_code IS NULL AND NOT compensation_required "
            "AND lease_expires_at IS NOT NULL) "
            "OR (execution_state = 'retryable_failure' AND attempt_count >= 1 "
            "AND started_at IS NOT NULL AND completed_at IS NULL AND failed_at IS NOT NULL "
            "AND result_hash IS NULL AND failure_code IS NOT NULL AND NOT compensation_required "
            "AND lease_expires_at IS NULL) "
            "OR (execution_state = 'succeeded' AND attempt_count >= 1 AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND completed_at >= started_at AND failed_at IS NULL "
            "AND result_hash IS NOT NULL AND failure_code IS NULL AND NOT compensation_required "
            "AND lease_expires_at IS NULL) "
            "OR (execution_state = 'permanent_failure' AND attempt_count >= 1 "
            "AND started_at IS NOT NULL AND completed_at IS NULL AND failed_at IS NOT NULL "
            "AND result_hash IS NULL AND failure_code IS NOT NULL AND compensation_required "
            "AND lease_expires_at IS NULL) "
            "OR (execution_state = 'reconciliation_required' AND attempt_count >= 1 "
            "AND started_at IS NOT NULL AND completed_at IS NULL AND failed_at IS NOT NULL "
            "AND result_hash IS NULL AND failure_code IS NOT NULL AND NOT compensation_required "
            "AND lease_expires_at IS NULL))",
            name="state_payload",
        ),
        CheckConstraint(
            "length(input_hash) = 71 AND substr(input_hash, 1, 7) = 'sha256:' "
            "AND input_hash = lower(input_hash)",
            name="input_hash_shape",
        ),
        CheckConstraint(
            "result_hash IS NULL OR (length(result_hash) = 71 "
            "AND substr(result_hash, 1, 7) = 'sha256:' AND result_hash = lower(result_hash))",
            name="result_hash_shape",
        ),
        CheckConstraint("id ~ '^ful_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="id_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint("input_hash ~ '^sha256:[0-9a-f]{64}$'", name="input_hash_format").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(
            "result_hash IS NULL OR result_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="result_hash_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "result_content_type IS NULL OR result_content_type ~* "
            "'^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$'",
            name="result_content_type_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "failure_code IS NULL OR failure_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
            name="failure_code_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "fulfillment_config_id IS NULL OR "
            "fulfillment_config_id ~ '^sfc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="fulfillment_config_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "endpoint_url IS NULL OR endpoint_url ~ '^https?://[^[:space:]]+$'",
            name="endpoint_url_format",
        ).ddl_if(dialect="postgresql"),
        Index("ix_fulfillment_executions_transaction", "transaction_id"),
        Index("ix_fulfillment_executions_account_created_at", "account_id", "created_at"),
        Index("ix_fulfillment_executions_state_updated_at", "execution_state", "updated_at"),
        Index(
            "ix_fulfillment_executions_lease_expiry",
            "lease_expires_at",
            postgresql_where=text("execution_state = 'executing'"),
            sqlite_where=text("execution_state = 'executing'"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String(30), primary_key=True, default=new_fulfillment_execution_id
    )
    entitlement_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("entitlements.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        String(31), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("payment_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    input_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    fulfillment_config_id: Mapped[str | None] = mapped_column(
        String(30),
        ForeignKey("service_fulfillment_configs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    fulfillment_config_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider_type: Mapped[FulfillmentProviderType | None] = mapped_column(
        Enum(
            FulfillmentProviderType,
            name="fulfillment_provider_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=True,
    )
    endpoint_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    request_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maximum_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_state: Mapped[FulfillmentExecutionState] = mapped_column(
        Enum(
            FulfillmentExecutionState,
            name="fulfillment_execution_state",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=FulfillmentExecutionState.PENDING,
        server_default=FulfillmentExecutionState.PENDING.value,
        active_history=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result_json: Mapped[Any | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"), nullable=True
    )
    result_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    result_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    compensation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    lease_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


@event.listens_for(FulfillmentExecution, "before_update")
def _guard_fulfillment_execution_update(
    _mapper: object,
    _connection: object,
    execution: FulfillmentExecution,
) -> None:
    state = sqlalchemy_inspect(execution)
    immutable = (
        "id",
        "entitlement_id",
        "account_id",
        "transaction_id",
        "merchant_id",
        "service_id",
        "input_hash",
        "created_at",
    )
    if any(state.attrs[field].history.has_changes() for field in immutable):
        raise InvalidRequestError("Fulfillment execution bindings are immutable")

    route_snapshot_fields = (
        "fulfillment_config_id",
        "fulfillment_config_revision",
        "provider_type",
        "endpoint_url",
        "request_timeout_seconds",
        "maximum_attempts",
    )
    for field in route_snapshot_fields:
        history = state.attrs[field].history
        if history.has_changes() and history.deleted and history.deleted[0] is not None:
            raise InvalidRequestError("Fulfillment execution route snapshot is immutable")
    route_values = [getattr(execution, field) for field in route_snapshot_fields]
    if any(value is not None for value in route_values) and any(
        value is None for value in route_values
    ):
        raise InvalidRequestError("Fulfillment execution route snapshot must be complete")

    revision_history = state.attrs.revision.history
    if not revision_history.has_changes() or not revision_history.deleted:
        raise InvalidRequestError("Fulfillment execution revision must advance exactly once")
    if execution.revision != revision_history.deleted[0] + 1:
        raise InvalidRequestError("Fulfillment execution revision must advance exactly once")

    state_history = state.attrs.execution_state.history
    prior_state = FulfillmentExecutionState(
        state_history.deleted[0]
        if state_history.has_changes() and state_history.deleted
        else execution.execution_state
    )
    resulting_state = FulfillmentExecutionState(execution.execution_state)
    if prior_state is FulfillmentExecutionState.SUCCEEDED and any(
        state.attrs[field].history.has_changes()
        for field in (
            "completed_at",
            "result_content_type",
            "result_json",
            "result_hash",
            "result_size_bytes",
        )
    ):
        raise InvalidRequestError("Successful fulfillment result evidence is immutable")
    if prior_state in {
        FulfillmentExecutionState.SUCCEEDED,
        FulfillmentExecutionState.PERMANENT_FAILURE,
    }:
        raise InvalidRequestError("Terminal fulfillment execution evidence is immutable")
    if (
        prior_state is FulfillmentExecutionState.RECONCILIATION_REQUIRED
        and resulting_state is FulfillmentExecutionState.RECONCILIATION_REQUIRED
    ):
        raise InvalidRequestError(
            "Fulfillment reconciliation evidence requires an explicit recovery transition"
        )
    if state_history.has_changes() and state_history.deleted:
        if not can_transition_fulfillment(prior_state, resulting_state):
            raise InvalidRequestError("Fulfillment execution state transition is not allowed")

    for field in ("attempt_count", "lease_generation"):
        history = state.attrs[field].history
        if (
            history.has_changes()
            and history.deleted
            and getattr(execution, field) < history.deleted[0]
        ):
            raise InvalidRequestError(f"Fulfillment execution {field} cannot regress")

    completed_history = state.attrs.completed_at.history
    if completed_history.has_changes() and completed_history.deleted:
        prior = completed_history.deleted[0]
        if prior is not None and execution.completed_at != prior:
            raise InvalidRequestError("Fulfillment completion evidence is immutable")


@event.listens_for(FulfillmentExecution, "before_delete")
def _reject_fulfillment_execution_delete(*_: object) -> None:
    raise InvalidRequestError("Fulfillment executions cannot be deleted")
