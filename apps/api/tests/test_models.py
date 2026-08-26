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
    text,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from app.domain.enums import (
    AccountStatus,
    ApprovalIdentityStatus,
    MerchantStatus,
    PaymentAttemptStatus,
    PaymentEventActorType,
    PaymentProvider,
    PaymentTransactionEventType,
    PaymentTransactionState,
    PolicyDecision,
    PurchaseType,
    RazorpayOrderStatus,
    ServiceStatus,
    WebhookProcessingStatus,
)
from app.domain.payment_hashing import (
    PAYMENT_BINDING_VERSION,
    calculate_payment_binding_hash,
)
from app.models import (
    Account,
    ApprovalIdentity,
    Base,
    BuyerPolicy,
    Merchant,
    PaymentAttempt,
    PaymentTransaction,
    PaymentTransactionEvent,
    PolicyEvaluation,
    Quote,
    RazorpayWebhookEvent,
    Service,
)
from app.repositories import BuyerPolicyRepository, PolicyEvaluationRepository

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


def policy_for_test(*, hash_value: int = 201) -> BuyerPolicy:
    issued_at = datetime(2026, 8, 25, 12, tzinfo=UTC)
    return BuyerPolicy(
        subject_ref="dev-user-001",
        maximum_amount=1_000,
        allowed_currencies=["INR"],
        allowed_merchant_ids=None,
        allowed_service_ids=["svc_00000000000000000000000000"],
        allowed_service_types=["report"],
        allowed_purchase_types=["one_time"],
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=15),
        policy_version="1",
        policy_hash=f"{HASH_PREFIX}{hash_value:064x}",
    )


def evaluation_for_test(*, hash_value: int = 301) -> PolicyEvaluation:
    return PolicyEvaluation(
        policy_id="pol_00000000000000000000000000",
        quote_id="qte_00000000000000000000000000",
        policy_hash=f"{HASH_PREFIX}{hash_value:064x}",
        quote_hash=f"{HASH_PREFIX}{hash_value + 1:064x}",
        decision=PolicyDecision.ALLOW,
        checks=[
            {
                "rule": "MAXIMUM_AMOUNT",
                "result": "pass",
                "reason_code": "ALLOW_POLICY_SATISFIED",
            }
        ],
        evaluated_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        evaluation_version="1",
    )


def payment_transaction_for_test(*, hash_value: int = 401) -> PaymentTransaction:
    transaction_id = "txn_00000000000000000000000001"
    fields = {
        "transaction_id": transaction_id,
        "payment_binding_version": PAYMENT_BINDING_VERSION,
        "account_id": "acct_00000000000000000000000001",
        "authorization_id": "aut_00000000000000000000000001",
        "authorization_hash": f"{HASH_PREFIX}{hash_value:064x}",
        "evaluation_id": "pye_00000000000000000000000001",
        "policy_id": "pol_00000000000000000000000001",
        "policy_hash": f"{HASH_PREFIX}{hash_value + 1:064x}",
        "quote_id": "qte_00000000000000000000000001",
        "quote_hash": f"{HASH_PREFIX}{hash_value + 2:064x}",
        "merchant_id": "mrc_00000000000000000000000001",
        "service_id": "svc_00000000000000000000000001",
        "amount": 500,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "provider": PaymentProvider.RAZORPAY,
        "provider_receipt": transaction_id,
    }
    return PaymentTransaction(
        id=transaction_id,
        account_id=fields["account_id"],
        authorization_id=fields["authorization_id"],
        authorization_hash=fields["authorization_hash"],
        evaluation_id=fields["evaluation_id"],
        policy_id=fields["policy_id"],
        policy_hash=fields["policy_hash"],
        quote_id=fields["quote_id"],
        quote_hash=fields["quote_hash"],
        merchant_id=fields["merchant_id"],
        service_id=fields["service_id"],
        amount=fields["amount"],
        currency=fields["currency"],
        purchase_type=fields["purchase_type"],
        provider=fields["provider"],
        provider_receipt=fields["provider_receipt"],
        transaction_state=PaymentTransactionState.ORDER_CREATION_PENDING,
        order_creation_attempts=1,
        order_creation_started_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        payment_binding_version=PAYMENT_BINDING_VERSION,
        payment_binding_hash=calculate_payment_binding_hash(**fields),
        revision=1,
    )


def test_models_use_prefixed_string_primary_keys() -> None:
    account_id_column = Account.__table__.c.id
    assert account_id_column.primary_key is True
    assert isinstance(account_id_column.type, String)
    assert account_id_column.type.length == 31

    for table in (
        BuyerPolicy.__table__,
        Merchant.__table__,
        PaymentAttempt.__table__,
        PaymentTransaction.__table__,
        PaymentTransactionEvent.__table__,
        PolicyEvaluation.__table__,
        Quote.__table__,
        RazorpayWebhookEvent.__table__,
        Service.__table__,
    ):
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


def test_policy_money_hash_and_json_columns_use_safe_database_types() -> None:
    policy_table = BuyerPolicy.__table__
    evaluation_table = PolicyEvaluation.__table__

    assert isinstance(policy_table.c.maximum_amount.type, BigInteger)
    assert isinstance(policy_table.c.policy_hash.type, String)
    assert policy_table.c.policy_hash.type.length == 71
    for column_name in (
        "allowed_currencies",
        "allowed_merchant_ids",
        "allowed_service_ids",
        "allowed_service_types",
        "allowed_purchase_types",
    ):
        column = policy_table.c[column_name]
        assert column.nullable is True
        assert str(column.type.compile(dialect=postgresql.dialect())) == "JSONB"
        assert str(column.type.compile(dialect=sqlite.dialect())) == "JSON"

    assert str(evaluation_table.c.checks.type.compile(dialect=postgresql.dialect())) == "JSONB"
    assert str(evaluation_table.c.checks.type.compile(dialect=sqlite.dialect())) == "JSON"
    assert evaluation_table.c.policy_hash.type.length == 71
    assert evaluation_table.c.quote_hash.type.length == 71


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


def test_policy_evaluation_foreign_keys_are_non_cascading() -> None:
    foreign_keys = {
        foreign_key.target_fullname: foreign_key.ondelete
        for column in (
            PolicyEvaluation.__table__.c.policy_id,
            PolicyEvaluation.__table__.c.quote_id,
        )
        for foreign_key in column.foreign_keys
    }

    assert foreign_keys == {
        "buyer_policies.id": "RESTRICT",
        "quotes.id": "RESTRICT",
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


def test_policy_models_define_integrity_constraints_and_query_indexes() -> None:
    policy_checks = {
        constraint.name
        for constraint in BuyerPolicy.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    evaluation_checks = {
        constraint.name
        for constraint in PolicyEvaluation.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    policy_unique = {
        constraint.name
        for constraint in BuyerPolicy.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert policy_unique == {"uq_buyer_policies_policy_hash"}
    assert {
        "ck_buyer_policies_policy_allowed_currencies_valid_allowlist",
        "ck_buyer_policies_policy_allowed_merchant_ids_valid_allowlist",
        "ck_buyer_policies_policy_allowed_purchase_types_valid_allowlist",
        "ck_buyer_policies_policy_allowed_service_ids_valid_allowlist",
        "ck_buyer_policies_policy_allowed_service_types_valid_allowlist",
        "ck_buyer_policies_policy_expiry_after_issue",
        "ck_buyer_policies_policy_hash_format",
        "ck_buyer_policies_policy_hash_shape",
        "ck_buyer_policies_policy_maximum_amount_safe_integer_range",
        "ck_buyer_policies_policy_version",
    } <= policy_checks
    allowlist_constraints = [
        constraint
        for constraint in BuyerPolicy.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name is not None
        and "valid_allowlist" in constraint.name
    ]
    assert len(allowlist_constraints) == 5
    assert all(
        "BETWEEN 1 AND 100" in str(constraint.sqltext) for constraint in allowlist_constraints
    )
    assert {index.name for index in BuyerPolicy.__table__.indexes} == {
        "ix_buyer_policies_expires_at",
        "ix_buyer_policies_subject_issued_at",
    }
    assert {
        "ck_policy_evaluations_checks_nonempty_object_array",
        "ck_policy_evaluations_policy_evaluation_decision",
        "ck_policy_evaluations_policy_evaluation_policy_hash_format",
        "ck_policy_evaluations_policy_evaluation_policy_hash_shape",
        "ck_policy_evaluations_policy_evaluation_quote_hash_format",
        "ck_policy_evaluations_policy_evaluation_quote_hash_shape",
        "ck_policy_evaluations_policy_evaluation_version",
    } <= evaluation_checks
    assert {index.name for index in PolicyEvaluation.__table__.indexes} == {
        "ix_policy_evaluations_decision_evaluated_at",
        "ix_policy_evaluations_policy_evaluated_at",
        "ix_policy_evaluations_quote_evaluated_at",
    }


def test_persisted_enum_values_are_lowercase_strings() -> None:
    enum_columns = (
        Account.__table__.c.status,
        Merchant.__table__.c.status,
        Service.__table__.c.status,
        Service.__table__.c.service_type,
        Service.__table__.c.purchase_type,
        Quote.__table__.c.purchase_type,
        PolicyEvaluation.__table__.c.decision,
        PaymentTransaction.__table__.c.provider,
        PaymentTransaction.__table__.c.provider_order_status,
        PaymentTransaction.__table__.c.transaction_state,
        PaymentAttempt.__table__.c.provider_status,
        RazorpayWebhookEvent.__table__.c.processing_status,
        PaymentTransactionEvent.__table__.c.event_type,
        PaymentTransactionEvent.__table__.c.actor_type,
        PaymentTransactionEvent.__table__.c.resulting_state,
    )
    for column in enum_columns:
        assert isinstance(column.type, Enum)
        assert all(value == value.lower() for value in column.type.enums)
        assert column.type.native_enum is False
        assert column.type.create_constraint is False


def test_account_model_defines_security_constraints_and_identity_binding() -> None:
    account_checks = {
        constraint.name
        for constraint in Account.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    identity_checks = {
        constraint.name
        for constraint in ApprovalIdentity.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    identity_unique = {
        constraint.name
        for constraint in ApprovalIdentity.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    account_foreign_key = next(iter(ApprovalIdentity.__table__.c.account_id.foreign_keys))

    assert Account.__table__.c.status.default.arg is AccountStatus.PENDING
    assert str(Account.__table__.c.status.server_default.arg) == "pending"
    assert Account.__table__.c.session_version.default.arg == 1
    assert str(Account.__table__.c.session_version.server_default.arg) == "1"
    assert {
        "ck_accounts_display_name_valid",
        "ck_accounts_id_format",
        "ck_accounts_id_length_prefix",
        "ck_accounts_session_version_positive",
        "ck_accounts_status",
        "ck_accounts_updated_at_valid",
    } <= account_checks
    assert {index.name for index in Account.__table__.indexes} == {"ix_accounts_status_created_at"}
    assert {
        "ck_approval_identities_account_id_format",
        "ck_approval_identities_account_id_length_prefix",
    } <= identity_checks
    assert "uq_approval_identities_account_id" in identity_unique
    assert account_foreign_key.target_fullname == "accounts.id"
    assert account_foreign_key.ondelete == "RESTRICT"
    assert ApprovalIdentity.__table__.c.account_id.nullable is False


def test_account_and_identity_mappers_enforce_account_security_guards() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    account_id = "acct_00000000000000000000000000"
    identity_id = "aid_00000000000000000000000000"

    with Session(engine, expire_on_commit=False) as session:
        account = Account(
            id=account_id,
            display_name="Account owner",
            status=AccountStatus.PENDING,
            session_version=1,
        )
        session.add(account)
        session.commit()

        account.status = AccountStatus.ACTIVE
        with pytest.raises(InvalidRequestError, match="session version must advance"):
            session.commit()
        session.rollback()

        account = session.get(Account, account_id)
        assert account is not None
        account.status = AccountStatus.ACTIVE
        account.session_version = 2
        session.commit()

        account.status = AccountStatus.PENDING
        account.session_version = 3
        with pytest.raises(InvalidRequestError, match="status transition"):
            session.commit()
        session.rollback()

        account = session.get(Account, account_id)
        assert account is not None
        identity = ApprovalIdentity(
            id=identity_id,
            account_id=account.id,
            subject_ref=account.id,
            display_name="Account owner",
            webauthn_user_handle=b"u" * 32,
            status=ApprovalIdentityStatus.ACTIVE,
        )
        session.add(identity)
        session.commit()

        identity.account_id = "acct_00000000000000000000000001"
        with pytest.raises(InvalidRequestError, match="bindings are immutable"):
            session.commit()
        session.rollback()

        account = session.get(Account, account_id)
        assert account is not None
        session.delete(account)
        with pytest.raises(InvalidRequestError, match="Accounts cannot be deleted"):
            session.commit()
        session.rollback()

        invalid_identity = ApprovalIdentity(
            id="aid_00000000000000000000000001",
            account_id=account_id,
            subject_ref="legacy-client-subject",
            display_name="Invalid binding",
            webauthn_user_handle=b"v" * 32,
            status=ApprovalIdentityStatus.ACTIVE,
        )
        session.add(invalid_identity)
        with pytest.raises(InvalidRequestError, match="subject must equal"):
            session.commit()
        session.rollback()

    engine.dispose()


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

    for table in (BuyerPolicy.__table__, PolicyEvaluation.__table__):
        assert "updated_at" not in table.c
        assert table.c.created_at.type.timezone is True
        assert table.c.created_at.server_default is not None
    for column_name in ("issued_at", "expires_at"):
        assert BuyerPolicy.__table__.c[column_name].type.timezone is True
        assert BuyerPolicy.__table__.c[column_name].server_default is None
    assert PolicyEvaluation.__table__.c.evaluated_at.type.timezone is True
    assert PolicyEvaluation.__table__.c.evaluated_at.server_default is None


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


@pytest.mark.parametrize(
    ("record_factory", "field_name", "new_value", "message"),
    [
        (policy_for_test, "maximum_amount", 500, "Buyer policies are immutable"),
        (evaluation_for_test, "decision", PolicyDecision.DENY, "Policy evaluations are immutable"),
    ],
)
def test_policy_mappers_reject_updates_and_deletes(
    record_factory: Any,
    field_name: str,
    new_value: Any,
    message: str,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        record = record_factory()
        session.add(record)
        session.commit()
        record_id = record.id

        setattr(record, field_name, new_value)
        with pytest.raises(InvalidRequestError, match=message):
            session.commit()
        session.rollback()

        persisted = session.get(type(record), record_id)
        assert persisted is not None
        session.delete(persisted)
        with pytest.raises(InvalidRequestError, match=message):
            session.commit()
        session.rollback()
    engine.dispose()


def test_policy_none_allowlist_is_stored_as_sql_null() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        policy = policy_for_test()
        session.add(policy)
        session.commit()

        is_sql_null = session.scalar(
            text("SELECT allowed_merchant_ids IS NULL FROM buyer_policies WHERE id = :policy_id"),
            {"policy_id": policy.id},
        )
        assert is_sql_null == 1
    engine.dispose()


def test_policy_repositories_expose_no_mutation_methods() -> None:
    for repository in (BuyerPolicyRepository, PolicyEvaluationRepository):
        assert not hasattr(repository, "update")
        assert not hasattr(repository, "delete")


def test_payment_models_define_safe_money_evidence_and_restrict_history() -> None:
    transaction_table = PaymentTransaction.__table__
    attempt_table = PaymentAttempt.__table__
    webhook_table = RazorpayWebhookEvent.__table__
    event_table = PaymentTransactionEvent.__table__

    assert isinstance(transaction_table.c.amount.type, BigInteger)
    assert isinstance(transaction_table.c.revision.type, BigInteger)
    assert transaction_table.c.provider_receipt.type.length == 40
    assert transaction_table.c.payment_binding_hash.type.length == 71
    assert isinstance(attempt_table.c.amount.type, BigInteger)
    assert isinstance(attempt_table.c.captured.type, Boolean)
    assert str(event_table.c.metadata.type.compile(dialect=postgresql.dialect())) == "JSONB"
    assert str(event_table.c.metadata.type.compile(dialect=sqlite.dialect())) == "JSON"

    transaction_uniques = {
        constraint.name
        for constraint in transaction_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert {
        "uq_payment_transactions_authorization_id",
        "uq_payment_transactions_id_provider_order_id",
        "uq_payment_transactions_payment_binding_hash",
        "uq_payment_transactions_provider_order_id",
        "uq_payment_transactions_provider_receipt",
    } <= transaction_uniques
    assert {index.name for index in transaction_table.indexes} == {
        "ix_payment_transactions_account_created_at",
        "ix_payment_transactions_merchant_created_at",
        "ix_payment_transactions_quote_created_at",
        "ix_payment_transactions_state_updated_at",
    }

    captured_index = next(
        index
        for index in attempt_table.indexes
        if index.name == "uq_payment_attempts_one_captured_per_transaction"
    )
    assert captured_index.unique is True
    assert "captured" in str(captured_index.dialect_options["postgresql"]["where"])
    assert {
        constraint.name
        for constraint in webhook_table.constraints
        if isinstance(constraint, UniqueConstraint)
    } == {"uq_razorpay_webhook_events_provider_event_id"}
    assert {
        constraint.name
        for constraint in event_table.constraints
        if isinstance(constraint, UniqueConstraint)
    } == {
        "uq_payment_transaction_events_idempotency_key",
        "uq_payment_transaction_events_source_webhook_event_id",
        "uq_payment_transaction_events_transaction_revision",
    }

    transaction_foreign_keys = {
        foreign_key.target_fullname: foreign_key.ondelete
        for column in transaction_table.c
        for foreign_key in column.foreign_keys
    }
    assert transaction_foreign_keys == {
        "accounts.id": "RESTRICT",
        "buyer_policies.id": "RESTRICT",
        "merchants.id": "RESTRICT",
        "policy_evaluations.id": "RESTRICT",
        "purchase_authorizations.id": "RESTRICT",
        "quotes.id": "RESTRICT",
        "services.id": "RESTRICT",
    }
    assert all(
        foreign_key.ondelete == "RESTRICT"
        for table in (attempt_table, webhook_table, event_table)
        for constraint in table.foreign_key_constraints
        for foreign_key in constraint.elements
    )


def test_payment_models_define_state_and_integrity_constraints() -> None:
    transaction_checks = {
        constraint.name
        for constraint in PaymentTransaction.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    attempt_checks = {
        constraint.name
        for constraint in PaymentAttempt.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    event_checks = {
        constraint.name
        for constraint in PaymentTransactionEvent.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {
        "ck_payment_transactions_amount_safe_integer_range",
        "ck_payment_transactions_authorization_hash_format",
        "ck_payment_transactions_binding_version",
        "ck_payment_transactions_paid_timestamp_matches_state",
        "ck_payment_transactions_payment_binding_hash_format",
        "ck_payment_transactions_provider_order_binding_complete",
        "ck_payment_transactions_provider_receipt_is_id",
        "ck_payment_transactions_revision_positive",
        "ck_payment_transactions_state_requires_provider_order",
        "ck_payment_transactions_transaction_state",
    } <= transaction_checks
    assert {
        "ck_payment_attempts_amount_safe_integer_range",
        "ck_payment_attempts_captured_flag_matches_status",
        "ck_payment_attempts_captured_status_sets_flag",
        "ck_payment_attempts_provider_status",
    } <= attempt_checks
    assert {
        "ck_payment_transaction_events_metadata_object",
        "ck_payment_transaction_events_prior_state_matches_revision",
        "ck_payment_transaction_events_reason_code_format",
        "ck_payment_transaction_events_transaction_revision_positive",
    } <= event_checks


def test_payment_mapper_guards_prevent_binding_changes_and_state_regression() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    started_at = datetime(2026, 8, 25, 12, tzinfo=UTC)

    with Session(engine, expire_on_commit=False) as session:
        transaction = payment_transaction_for_test()
        session.add(transaction)
        session.commit()

        transaction.amount = 501
        transaction.revision = 2
        with pytest.raises(InvalidRequestError, match="binding fields are immutable"):
            session.commit()
        session.rollback()

        transaction = session.get(PaymentTransaction, transaction.id)
        assert transaction is not None
        transaction.transaction_state = PaymentTransactionState.PAID
        transaction.paid_at = started_at + timedelta(seconds=3)
        transaction.revision = 2
        with pytest.raises(InvalidRequestError, match="state transition"):
            session.commit()
        session.rollback()

        transaction = session.get(PaymentTransaction, transaction.id)
        assert transaction is not None
        transaction.provider_order_id = "order_test001"
        transaction.provider_order_status = RazorpayOrderStatus.CREATED
        transaction.order_created_at = started_at + timedelta(seconds=1)
        transaction.transaction_state = PaymentTransactionState.ORDER_CREATED
        transaction.revision = 2
        session.commit()

        attempt = PaymentAttempt(
            id="pmt_00000000000000000000000001",
            transaction_id=transaction.id,
            provider=PaymentProvider.RAZORPAY,
            provider_order_id="order_test001",
            provider_payment_id="pay_test001",
            amount=500,
            currency="INR",
            provider_status=PaymentAttemptStatus.FAILED,
            method="upi",
            captured=False,
            provider_created_at=started_at,
            first_seen_at=started_at,
            last_seen_at=started_at,
        )
        session.add(attempt)
        session.commit()

        attempt.provider_status = PaymentAttemptStatus.AUTHORIZED
        attempt.last_seen_at = started_at + timedelta(seconds=2)
        session.commit()
        attempt.provider_status = PaymentAttemptStatus.CAPTURED
        attempt.captured = True
        attempt.last_seen_at = started_at + timedelta(seconds=3)
        session.commit()
        attempt.provider_status = PaymentAttemptStatus.AUTHORIZED
        attempt.last_seen_at = started_at + timedelta(seconds=4)
        with pytest.raises(InvalidRequestError, match="status transition"):
            session.commit()
        session.rollback()

        transaction = session.get(PaymentTransaction, transaction.id)
        assert transaction is not None
        session.delete(transaction)
        with pytest.raises(InvalidRequestError, match="cannot be deleted"):
            session.commit()
        session.rollback()

    engine.dispose()


def test_payment_audit_evidence_mappers_are_append_only() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    occurred_at = datetime(2026, 8, 25, 12, tzinfo=UTC)

    with Session(engine, expire_on_commit=False) as session:
        webhook = RazorpayWebhookEvent(
            id="rwe_00000000000000000000000001",
            provider_event_id="event_test001",
            provider_event_type="payment.captured",
            raw_body_hash=f"{HASH_PREFIX}{501:064x}",
            provider_created_at=occurred_at,
            received_at=occurred_at,
            processed_at=occurred_at,
            processing_status=WebhookProcessingStatus.IGNORED,
            processing_reason_code="PAYMENT_TRANSACTION_NOT_FOUND",
        )
        event = PaymentTransactionEvent(
            id="pte_00000000000000000000000001",
            transaction_id="txn_00000000000000000000000001",
            transaction_revision=1,
            event_type=PaymentTransactionEventType.PAYMENT_TRANSACTION_CREATED,
            actor_type=PaymentEventActorType.ACCOUNT,
            actor_id="acct_00000000000000000000000001",
            prior_state=None,
            resulting_state=PaymentTransactionState.ORDER_CREATION_PENDING,
            reason_code="PAYMENT_TRANSACTION_CREATED",
            event_metadata={},
            idempotency_key="transaction:txn_00000000000000000000000001:revision:1",
            occurred_at=occurred_at,
        )
        session.add_all((webhook, event))
        session.commit()

        webhook.processing_reason_code = "CHANGED"
        with pytest.raises(InvalidRequestError, match="webhook evidence is immutable"):
            session.commit()
        session.rollback()

        event = session.get(PaymentTransactionEvent, event.id)
        assert event is not None
        session.delete(event)
        with pytest.raises(InvalidRequestError, match="events are immutable"):
            session.commit()
        session.rollback()

    engine.dispose()
