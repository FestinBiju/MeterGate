from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from app.domain.enums import MerchantStatus, PurchaseType, ServiceStatus
from app.models import Base, Merchant, Quote, Service

HASH_PREFIX = "sha256:"


def quote_for_test(input_value: Any, *, hash_value: int = 1) -> Quote:
    issued_at = datetime(2026, 8, 25, 12, tzinfo=UTC)
    return Quote(
        merchant_id="mrc_00000000000000000000000000",
        service_id="svc_00000000000000000000000000",
        input=input_value,
        input_hash=f"{HASH_PREFIX}{hash_value:064x}",
        service_snapshot={"service": {"version": "test"}},
        amount=500,
        currency="INR",
        purchase_type=PurchaseType.ONE_TIME,
        maximum_fulfillment_seconds=30,
        refund_on_fulfillment_failure=True,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=5),
        quote_hash=f"{HASH_PREFIX}{hash_value + 100:064x}",
    )


def test_models_use_prefixed_string_primary_keys() -> None:
    for table in (Merchant.__table__, Quote.__table__, Service.__table__):
        id_column = table.c.id
        assert id_column.primary_key is True
        assert isinstance(id_column.type, String)
        assert id_column.type.length == 30
        assert id_column.autoincrement is not True


def test_only_status_fields_have_domain_defaults() -> None:
    assert Merchant.__table__.c.status.default.arg is MerchantStatus.ACTIVE
    assert str(Merchant.__table__.c.status.server_default.arg) == "active"
    assert Service.__table__.c.status.default.arg is ServiceStatus.DRAFT
    assert str(Service.__table__.c.status.server_default.arg) == "draft"

    required_service_fields = (
        "currency",
        "base_price",
        "input_schema",
        "output_schema",
        "output_content_type",
        "maximum_fulfillment_seconds",
        "refund_on_fulfillment_failure",
    )
    for field_name in required_service_fields:
        column = Service.__table__.c[field_name]
        assert column.nullable is False
        assert column.default is None
        assert column.server_default is None


def test_service_money_and_json_columns_use_safe_database_types() -> None:
    assert isinstance(Service.__table__.c.base_price.type, BigInteger)
    input_schema_type = Service.__table__.c.input_schema.type
    output_schema_type = Service.__table__.c.output_schema.type

    assert str(input_schema_type.compile(dialect=postgresql.dialect())) == "JSONB"
    assert str(output_schema_type.compile(dialect=postgresql.dialect())) == "JSONB"
    assert str(input_schema_type.compile(dialect=sqlite.dialect())) == "JSON"


def test_quote_money_hash_and_json_columns_use_safe_database_types() -> None:
    table = Quote.__table__

    assert isinstance(table.c.amount.type, BigInteger)
    assert isinstance(table.c.maximum_fulfillment_seconds.type, Integer)
    assert isinstance(table.c.refund_on_fulfillment_failure.type, Boolean)
    assert isinstance(table.c.input_hash.type, String)
    assert table.c.input_hash.type.length == 71
    assert isinstance(table.c.quote_hash.type, String)
    assert table.c.quote_hash.type.length == 71
    assert str(table.c.input.type.compile(dialect=postgresql.dialect())) == "JSONB"
    assert str(table.c.input.type.compile(dialect=sqlite.dialect())) == "JSON"
    assert str(table.c.service_snapshot.type.compile(dialect=postgresql.dialect())) == "JSONB"


def test_service_foreign_key_restricts_merchant_deletion() -> None:
    foreign_key = next(iter(Service.__table__.c.merchant_id.foreign_keys))

    assert foreign_key.target_fullname == "merchants.id"
    assert foreign_key.ondelete == "RESTRICT"


def test_quote_foreign_keys_are_non_cascading() -> None:
    foreign_keys = {
        foreign_key.target_fullname: foreign_key.ondelete
        for column in (Quote.__table__.c.merchant_id, Quote.__table__.c.service_id)
        for foreign_key in column.foreign_keys
    }

    assert foreign_keys == {
        "merchants.id": "RESTRICT",
        "services.id": "RESTRICT",
    }


def test_models_define_uniqueness_checks_and_catalog_indexes() -> None:
    merchant_unique = {
        constraint.name
        for constraint in Merchant.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    service_unique = {
        constraint.name
        for constraint in Service.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    service_checks = {
        constraint.name
        for constraint in Service.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "uq_merchants_slug" in merchant_unique
    assert "uq_services_merchant_id_slug" in service_unique
    assert {
        "ck_services_service_base_price_nonnegative",
        "ck_services_service_fulfillment_seconds_range",
        "ck_services_service_input_schema_object",
        "ck_services_service_output_schema_object",
        "ck_services_service_status",
        "ck_services_service_type",
        "ck_services_purchase_type",
    } <= service_checks
    assert {index.name for index in Service.__table__.indexes} == {
        "ix_services_catalog",
        "ix_services_merchant_list",
    }


def test_quote_defines_integrity_constraints_and_indexes() -> None:
    unique_constraints = {
        constraint.name
        for constraint in Quote.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    checks = {
        constraint.name
        for constraint in Quote.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert unique_constraints == {"uq_quotes_quote_hash"}
    assert {
        "ck_quotes_purchase_type",
        "ck_quotes_quote_amount_safe_integer_range",
        "ck_quotes_quote_expiry_after_issue",
        "ck_quotes_quote_fulfillment_seconds_range",
        "ck_quotes_quote_hash_format",
        "ck_quotes_quote_hash_shape",
        "ck_quotes_quote_input_hash_format",
        "ck_quotes_quote_input_hash_shape",
        "ck_quotes_quote_service_snapshot_object",
    } <= checks
    assert {index.name for index in Quote.__table__.indexes} == {
        "ix_quotes_expires_at",
        "ix_quotes_merchant_issued_at",
        "ix_quotes_service_issued_at",
    }


def test_persisted_enum_values_are_lowercase_strings() -> None:
    enum_columns = (
        Merchant.__table__.c.status,
        Service.__table__.c.status,
        Service.__table__.c.service_type,
        Service.__table__.c.purchase_type,
        Quote.__table__.c.purchase_type,
    )
    for column in enum_columns:
        assert isinstance(column.type, Enum)
        assert all(value == value.lower() for value in column.type.enums)
        assert column.type.native_enum is False
        assert column.type.create_constraint is False


def test_timestamps_are_timezone_aware() -> None:
    for table in (Merchant.__table__, Service.__table__):
        assert table.c.created_at.type.timezone is True
        assert table.c.updated_at.type.timezone is True
        assert table.c.created_at.server_default is not None
        assert table.c.updated_at.server_default is not None

    assert "updated_at" not in Quote.__table__.c
    for column_name in ("issued_at", "expires_at", "created_at"):
        assert Quote.__table__.c[column_name].type.timezone is True
    assert Quote.__table__.c.issued_at.server_default is None
    assert Quote.__table__.c.expires_at.server_default is None
    assert Quote.__table__.c.created_at.server_default is not None


@pytest.mark.parametrize(
    "input_value",
    [None, [], [1, "two"], 7, "value", True, {"key": "value"}],
)
def test_quote_input_round_trips_every_json_value_type(input_value: Any) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        quote = quote_for_test(input_value)
        session.add(quote)
        session.commit()
        session.refresh(quote)

        assert quote.input == input_value
    engine.dispose()


def test_quote_mapper_rejects_updates_and_deletes() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        quote = quote_for_test({"norad_id": 25544})
        session.add(quote)
        session.commit()
        quote_id = quote.id

        quote.amount = 700
        with pytest.raises(InvalidRequestError, match="Quotes are immutable"):
            session.commit()
        session.rollback()

        persisted = session.get(Quote, quote_id)
        assert persisted is not None
        session.delete(persisted)
        with pytest.raises(InvalidRequestError, match="Quotes are immutable"):
            session.commit()
        session.rollback()
    engine.dispose()
