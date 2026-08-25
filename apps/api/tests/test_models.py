from sqlalchemy import BigInteger, CheckConstraint, Enum, String, UniqueConstraint
from sqlalchemy.dialects import postgresql, sqlite

from app.domain.enums import MerchantStatus, ServiceStatus
from app.models import Merchant, Service


def test_models_use_prefixed_string_primary_keys() -> None:
    for table in (Merchant.__table__, Service.__table__):
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


def test_service_foreign_key_restricts_merchant_deletion() -> None:
    foreign_key = next(iter(Service.__table__.c.merchant_id.foreign_keys))

    assert foreign_key.target_fullname == "merchants.id"
    assert foreign_key.ondelete == "RESTRICT"


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


def test_persisted_enum_values_are_lowercase_strings() -> None:
    enum_columns = (
        Merchant.__table__.c.status,
        Service.__table__.c.status,
        Service.__table__.c.service_type,
        Service.__table__.c.purchase_type,
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
