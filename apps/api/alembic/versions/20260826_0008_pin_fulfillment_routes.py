"""Pin the merchant route used by each fulfillment execution.

Revision ID: 20260826_0008
Revises: 20260826_0007
Create Date: 2026-08-26
"""

import secrets
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "20260826_0008"
down_revision: str | None = "20260826_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TERMINAL_STATES = ("succeeded", "permanent_failure")
_ROUTE_QUARANTINE_CODE = "FULFILLMENT_ROUTE_PROVENANCE_UNKNOWN"


def _encode_crockford(value: int) -> str:
    characters = ["0"] * 26
    for index in range(25, -1, -1):
        value, remainder = divmod(value, 32)
        characters[index] = _CROCKFORD_ALPHABET[remainder]
    if value:
        raise OverflowError("Migration identifier payload exceeds its encoded length")
    return "".join(characters)


def _new_fulfillment_event_id() -> str:
    """Generate a migration-local ULID-shaped event ID without importing app code."""
    timestamp_ms = time.time_ns() // 1_000_000
    payload = (timestamp_ms << 80) | secrets.randbits(80)
    return f"fve_{_encode_crockford(payload)}"


def _drop_postgresql_execution_guard_trigger() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_fulfillment_executions_guard ON fulfillment_executions"
        )


def _install_postgresql_execution_guard(*, include_route_snapshot: bool) -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    route_guard = ""
    if include_route_snapshot:
        route_guard = """
            IF OLD.fulfillment_config_id IS NOT NULL
               AND ROW(NEW.fulfillment_config_id, NEW.fulfillment_config_revision,
                       NEW.provider_type, NEW.endpoint_url,
                       NEW.request_timeout_seconds, NEW.maximum_attempts)
                   IS DISTINCT FROM
                   ROW(OLD.fulfillment_config_id, OLD.fulfillment_config_revision,
                       OLD.provider_type, OLD.endpoint_url,
                       OLD.request_timeout_seconds, OLD.maximum_attempts) THEN
                RAISE EXCEPTION 'fulfillment execution route snapshot is immutable'
                    USING ERRCODE = '55000';
            END IF;
        """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION metergate_guard_fulfillment_execution()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'fulfillment executions cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF ROW(NEW.id, NEW.entitlement_id, NEW.account_id, NEW.transaction_id,
                   NEW.merchant_id, NEW.service_id, NEW.input_hash, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.entitlement_id, OLD.account_id, OLD.transaction_id,
                   OLD.merchant_id, OLD.service_id, OLD.input_hash, OLD.created_at) THEN
                RAISE EXCEPTION 'fulfillment execution bindings are immutable'
                    USING ERRCODE = '55000';
            END IF;
            {route_guard}
            IF OLD.execution_state = 'succeeded'
               AND ROW(NEW.completed_at, NEW.result_content_type, NEW.result_json,
                       NEW.result_hash, NEW.result_size_bytes)
                   IS DISTINCT FROM
                   ROW(OLD.completed_at, OLD.result_content_type, OLD.result_json,
                       OLD.result_hash, OLD.result_size_bytes) THEN
                RAISE EXCEPTION 'successful fulfillment result evidence is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.execution_state IN ('succeeded', 'permanent_failure')
               AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'terminal fulfillment execution evidence is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.execution_state = 'reconciliation_required'
               AND NEW.execution_state = 'reconciliation_required'
               AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION
                    'fulfillment reconciliation evidence requires an explicit recovery transition'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'fulfillment execution revision must advance exactly once'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.attempt_count < OLD.attempt_count
               OR NEW.lease_generation < OLD.lease_generation THEN
                RAISE EXCEPTION 'fulfillment execution counters cannot regress'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.execution_state <> OLD.execution_state AND NOT (
                (OLD.execution_state = 'pending' AND NEW.execution_state IN
                    ('executing', 'permanent_failure', 'reconciliation_required')) OR
                (OLD.execution_state = 'executing' AND NEW.execution_state IN
                    ('retryable_failure', 'succeeded', 'permanent_failure',
                     'reconciliation_required')) OR
                (OLD.execution_state = 'retryable_failure' AND NEW.execution_state IN
                    ('executing', 'permanent_failure', 'reconciliation_required')) OR
                (OLD.execution_state = 'reconciliation_required' AND NEW.execution_state IN
                    ('executing', 'succeeded', 'permanent_failure'))
            ) THEN
                RAISE EXCEPTION 'fulfillment execution state transition is not allowed'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_fulfillment_executions_guard
        BEFORE UPDATE OR DELETE ON fulfillment_executions
        FOR EACH ROW EXECUTE FUNCTION metergate_guard_fulfillment_execution()
        """
    )


def _route_is_proven(
    event_metadata: Sequence[object],
    config: Mapping[str, Any] | None,
) -> bool:
    """Require every prior send to prove the same still-current config revision."""
    if config is None or not event_metadata:
        return False
    expected = (config["id"], config["revision"])
    for metadata in event_metadata:
        if not isinstance(metadata, Mapping):
            return False
        config_id = metadata.get("fulfillment_config_id")
        config_revision = metadata.get("fulfillment_config_revision")
        if (
            not isinstance(config_id, str)
            or type(config_revision) is not int
            or (config_id, config_revision) != expected
        ):
            return False
    return True


def _backfill_or_quarantine_dispatched_routes() -> None:
    """Recover only audit-proven routes; quarantine ambiguous prior dispatches."""
    connection = op.get_bind()
    executions = sa.table(
        "fulfillment_executions",
        sa.column("id", sa.String()),
        sa.column("entitlement_id", sa.String()),
        sa.column("transaction_id", sa.String()),
        sa.column("service_id", sa.String()),
        sa.column("execution_state", sa.String()),
        sa.column("revision", sa.BigInteger()),
        sa.column("failed_at", sa.DateTime(timezone=True)),
        sa.column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.column("compensation_required", sa.Boolean()),
        sa.column("failure_code", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("fulfillment_config_id", sa.String()),
        sa.column("fulfillment_config_revision", sa.BigInteger()),
        sa.column("provider_type", sa.String()),
        sa.column("endpoint_url", sa.String()),
        sa.column("request_timeout_seconds", sa.Integer()),
        sa.column("maximum_attempts", sa.Integer()),
    )
    events = sa.table(
        "fulfillment_events",
        sa.column("id", sa.String()),
        sa.column("transaction_id", sa.String()),
        sa.column("entitlement_id", sa.String()),
        sa.column("execution_id", sa.String()),
        sa.column("execution_revision", sa.BigInteger()),
        sa.column("event_type", sa.String()),
        sa.column("actor_type", sa.String()),
        sa.column("actor_id", sa.String()),
        sa.column("reason_code", sa.String()),
        sa.column("metadata", sa.JSON()),
        sa.column("idempotency_key", sa.String()),
        sa.column("occurred_at", sa.DateTime(timezone=True)),
    )
    configs = sa.table(
        "service_fulfillment_configs",
        sa.column("id", sa.String()),
        sa.column("service_id", sa.String()),
        sa.column("revision", sa.BigInteger()),
        sa.column("provider_type", sa.String()),
        sa.column("endpoint_url", sa.String()),
        sa.column("request_timeout_seconds", sa.Integer()),
        sa.column("maximum_attempts", sa.Integer()),
    )

    dispatched_rows = connection.execute(
        sa.select(
            executions.c.id,
            executions.c.entitlement_id,
            executions.c.transaction_id,
            executions.c.service_id,
            executions.c.execution_state,
            executions.c.revision,
            executions.c.failed_at,
        )
        .where(executions.c.execution_state.not_in(_TERMINAL_STATES))
        .where(
            sa.exists(
                sa.select(1).where(
                    events.c.execution_id == executions.c.id,
                    events.c.event_type == "merchant_request_sent",
                )
            )
        )
    ).mappings()
    rows = list(dispatched_rows)
    if not rows:
        return

    execution_ids = [row["id"] for row in rows]
    metadata_by_execution: dict[str, list[object]] = {item: [] for item in execution_ids}
    for row in connection.execute(
        sa.select(events.c.execution_id, events.c.metadata)
        .where(
            events.c.execution_id.in_(execution_ids),
            events.c.event_type == "merchant_request_sent",
        )
        .order_by(events.c.execution_id, events.c.occurred_at, events.c.id)
    ).mappings():
        metadata_by_execution[row["execution_id"]].append(row["metadata"])

    service_ids = {row["service_id"] for row in rows}
    config_by_service = {
        row["service_id"]: row
        for row in connection.execute(
            sa.select(configs).where(configs.c.service_id.in_(service_ids))
        ).mappings()
    }
    migration_time = datetime.now(UTC)

    for row in rows:
        config = config_by_service.get(row["service_id"])
        if _route_is_proven(metadata_by_execution[row["id"]], config):
            assert config is not None
            connection.execute(
                executions.update()
                .where(executions.c.id == row["id"])
                .values(
                    fulfillment_config_id=config["id"],
                    fulfillment_config_revision=config["revision"],
                    provider_type=config["provider_type"],
                    endpoint_url=config["endpoint_url"],
                    request_timeout_seconds=config["request_timeout_seconds"],
                    maximum_attempts=config["maximum_attempts"],
                )
            )
            continue

        new_revision = row["revision"] + 1
        connection.execute(
            executions.update()
            .where(executions.c.id == row["id"])
            .values(
                execution_state="permanent_failure",
                failed_at=row["failed_at"] or migration_time,
                failure_code=_ROUTE_QUARANTINE_CODE,
                compensation_required=True,
                lease_expires_at=None,
                revision=new_revision,
                updated_at=migration_time,
            )
        )
        event_metadata = {
            "failure_code": _ROUTE_QUARANTINE_CODE,
            "prior_execution_state": row["execution_state"],
            "migration_revision": revision,
        }
        for event_type, reason_code, suffix in (
            (
                "fulfillment_failed",
                "FULFILLMENT_PERMANENT_FAILURE",
                "fulfillment-failed",
            ),
            (
                "compensation_required",
                "FULFILLMENT_COMPENSATION_REQUIRED",
                "compensation-required",
            ),
        ):
            connection.execute(
                events.insert().values(
                    id=_new_fulfillment_event_id(),
                    transaction_id=row["transaction_id"],
                    entitlement_id=row["entitlement_id"],
                    execution_id=row["id"],
                    execution_revision=new_revision,
                    event_type=event_type,
                    actor_type="system",
                    actor_id=None,
                    reason_code=reason_code,
                    metadata=event_metadata,
                    idempotency_key=f"{row['id']}:{revision}:{suffix}",
                    occurred_at=migration_time,
                )
            )


def upgrade() -> None:
    provider_type = sa.Enum(
        "http",
        name="fulfillment_provider_type",
        native_enum=False,
        create_constraint=False,
    )
    with op.batch_alter_table("fulfillment_executions") as batch_op:
        batch_op.add_column(sa.Column("fulfillment_config_id", sa.String(30), nullable=True))
        batch_op.add_column(
            sa.Column("fulfillment_config_revision", sa.BigInteger(), nullable=True)
        )
        batch_op.add_column(sa.Column("provider_type", provider_type, nullable=True))
        batch_op.add_column(sa.Column("endpoint_url", sa.String(2048), nullable=True))
        batch_op.add_column(sa.Column("request_timeout_seconds", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("maximum_attempts", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_fulfillment_executions_fulfillment_config_id_service_fulfillment_configs"),
            "service_fulfillment_configs",
            ["fulfillment_config_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            op.f("ck_fulfillment_executions_route_snapshot_complete"),
            "((fulfillment_config_id IS NULL AND fulfillment_config_revision IS NULL "
            "AND provider_type IS NULL AND endpoint_url IS NULL "
            "AND request_timeout_seconds IS NULL AND maximum_attempts IS NULL) OR "
            "(fulfillment_config_id IS NOT NULL AND fulfillment_config_revision IS NOT NULL "
            "AND provider_type IS NOT NULL AND endpoint_url IS NOT NULL "
            "AND request_timeout_seconds IS NOT NULL AND maximum_attempts IS NOT NULL))",
        )
        batch_op.create_check_constraint(
            op.f("ck_fulfillment_executions_fulfillment_config_revision_positive"),
            "fulfillment_config_revision IS NULL OR fulfillment_config_revision >= 1",
        )
        batch_op.create_check_constraint(
            op.f("ck_fulfillment_executions_provider_type"),
            "provider_type IS NULL OR provider_type = 'http'",
        )
        batch_op.create_check_constraint(
            op.f("ck_fulfillment_executions_endpoint_url_valid"),
            "endpoint_url IS NULL OR (length(endpoint_url) BETWEEN 1 AND 2048 "
            "AND endpoint_url = trim(endpoint_url))",
        )
        batch_op.create_check_constraint(
            op.f("ck_fulfillment_executions_request_timeout_seconds_range"),
            "request_timeout_seconds IS NULL OR request_timeout_seconds BETWEEN 1 AND 60",
        )
        batch_op.create_check_constraint(
            op.f("ck_fulfillment_executions_maximum_attempts_range"),
            "maximum_attempts IS NULL OR maximum_attempts BETWEEN 1 AND 10",
        )
        if op.get_context().dialect.name == "postgresql":
            batch_op.create_check_constraint(
                op.f("ck_fulfillment_executions_fulfillment_config_id_format"),
                "fulfillment_config_id IS NULL OR "
                "fulfillment_config_id ~ '^sfc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            )
            batch_op.create_check_constraint(
                op.f("ck_fulfillment_executions_endpoint_url_format"),
                "endpoint_url IS NULL OR endpoint_url ~ '^https?://[^[:space:]]+$'",
            )

    # 0007's execution trigger predates route columns and rejects migration-time
    # enrichment (and every terminal UPDATE). Remove it only inside Alembic's
    # transaction, recover proven history, quarantine ambiguity, then reinstall
    # a stricter guard before application code can resume.
    _drop_postgresql_execution_guard_trigger()
    _backfill_or_quarantine_dispatched_routes()
    _install_postgresql_execution_guard(include_route_snapshot=True)


def downgrade() -> None:
    # Restore the exact 0007 guard before removing columns referenced by the 0008
    # function. Quarantine evidence is deliberately retained as durable history.
    _drop_postgresql_execution_guard_trigger()
    _install_postgresql_execution_guard(include_route_snapshot=False)
    with op.batch_alter_table("fulfillment_executions") as batch_op:
        if op.get_context().dialect.name == "postgresql":
            batch_op.drop_constraint(
                op.f("ck_fulfillment_executions_endpoint_url_format"),
                type_="check",
            )
            batch_op.drop_constraint(
                op.f("ck_fulfillment_executions_fulfillment_config_id_format"),
                type_="check",
            )
        batch_op.drop_constraint(
            op.f("ck_fulfillment_executions_maximum_attempts_range"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_fulfillment_executions_request_timeout_seconds_range"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_fulfillment_executions_endpoint_url_valid"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_fulfillment_executions_provider_type"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_fulfillment_executions_fulfillment_config_revision_positive"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_fulfillment_executions_route_snapshot_complete"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("fk_fulfillment_executions_fulfillment_config_id_service_fulfillment_configs"),
            type_="foreignkey",
        )
        batch_op.drop_column("maximum_attempts")
        batch_op.drop_column("request_timeout_seconds")
        batch_op.drop_column("endpoint_url")
        batch_op.drop_column("provider_type")
        batch_op.drop_column("fulfillment_config_revision")
        batch_op.drop_column("fulfillment_config_id")
