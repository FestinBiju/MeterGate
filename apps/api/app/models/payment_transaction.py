"""Stateful authorization-backed payment transaction aggregate."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    PaymentProvider,
    PaymentTransactionState,
    PurchaseType,
    RazorpayOrderStatus,
)
from app.domain.ids import new_payment_transaction_id
from app.domain.payment_hashing import recompute_payment_binding_hash
from app.domain.payment_state import (
    can_transition_order_status,
    can_transition_transaction,
)
from app.models.base import Base, TimestampMixin


def _hash_shape_constraint(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"length({column}) = 71 "
        f"AND substr({column}, 1, 7) = 'sha256:' "
        f"AND {column} = lower({column})",
        name=f"{column}_shape",
    )


def _hash_format_constraint(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} ~ '^sha256:[0-9a-f]{{64}}$'",
        name=f"{column}_format",
    ).ddl_if(dialect="postgresql")


class PaymentTransaction(TimestampMixin, Base):
    """MeterGate's durable claim of one immutable purchase authorization."""

    __tablename__ = "payment_transactions"
    __table_args__ = (
        UniqueConstraint(
            "authorization_id",
            name="uq_payment_transactions_authorization_id",
        ),
        UniqueConstraint(
            "provider_receipt",
            name="uq_payment_transactions_provider_receipt",
        ),
        UniqueConstraint(
            "provider_order_id",
            name="uq_payment_transactions_provider_order_id",
        ),
        UniqueConstraint(
            "payment_binding_hash",
            name="uq_payment_transactions_payment_binding_hash",
        ),
        UniqueConstraint(
            "id",
            "provider_order_id",
            name="uq_payment_transactions_id_provider_order_id",
        ),
        CheckConstraint(
            "length(id) = 30 AND substr(id, 1, 4) = 'txn_'",
            name="id_length_prefix",
        ),
        CheckConstraint(
            "length(account_id) = 31 AND substr(account_id, 1, 5) = 'acct_'",
            name="account_id_length_prefix",
        ),
        CheckConstraint(
            "length(authorization_id) = 30 AND substr(authorization_id, 1, 4) = 'aut_'",
            name="authorization_id_length_prefix",
        ),
        CheckConstraint(
            "length(evaluation_id) = 30 AND substr(evaluation_id, 1, 4) = 'pye_'",
            name="evaluation_id_length_prefix",
        ),
        CheckConstraint(
            "length(policy_id) = 30 AND substr(policy_id, 1, 4) = 'pol_'",
            name="policy_id_length_prefix",
        ),
        CheckConstraint(
            "length(quote_id) = 30 AND substr(quote_id, 1, 4) = 'qte_'",
            name="quote_id_length_prefix",
        ),
        CheckConstraint(
            "length(merchant_id) = 30 AND substr(merchant_id, 1, 4) = 'mrc_'",
            name="merchant_id_length_prefix",
        ),
        CheckConstraint(
            "length(service_id) = 30 AND substr(service_id, 1, 4) = 'svc_'",
            name="service_id_length_prefix",
        ),
        CheckConstraint(
            "amount BETWEEN 1 AND 9007199254740991",
            name="amount_safe_integer_range",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="currency_uppercase",
        ),
        CheckConstraint(
            "purchase_type IN ('one_time', 'subscription', 'usage_based')",
            name="purchase_type",
        ),
        CheckConstraint("provider = 'razorpay'", name="provider"),
        CheckConstraint(
            "length(provider_receipt) BETWEEN 1 AND 40 "
            "AND provider_receipt = trim(provider_receipt)",
            name="provider_receipt_valid",
        ),
        CheckConstraint("provider_receipt = id", name="provider_receipt_is_id"),
        CheckConstraint(
            "provider_order_id IS NULL OR "
            "(length(provider_order_id) BETWEEN 1 AND 64 "
            "AND provider_order_id = trim(provider_order_id))",
            name="provider_order_id_valid",
        ),
        CheckConstraint(
            "provider_order_status IS NULL "
            "OR provider_order_status IN ('created', 'attempted', 'paid')",
            name="provider_order_status",
        ),
        CheckConstraint(
            "transaction_state IN ("
            "'order_creation_pending', 'order_created', 'order_creation_failed', "
            "'order_creation_uncertain', 'payment_pending', 'payment_authorized', "
            "'paid', 'reconciliation_required')",
            name="transaction_state",
        ),
        CheckConstraint(
            "(provider_order_id IS NULL "
            "AND provider_order_status IS NULL AND order_created_at IS NULL) "
            "OR (provider_order_id IS NOT NULL "
            "AND provider_order_status IS NOT NULL AND order_created_at IS NOT NULL)",
            name="provider_order_binding_complete",
        ),
        CheckConstraint(
            "transaction_state NOT IN ("
            "'order_created', 'payment_pending', 'payment_authorized', 'paid') "
            "OR provider_order_id IS NOT NULL",
            name="state_requires_provider_order",
        ),
        CheckConstraint(
            "(transaction_state = 'paid' AND paid_at IS NOT NULL) "
            "OR (transaction_state <> 'paid' AND paid_at IS NULL)",
            name="paid_timestamp_matches_state",
        ),
        CheckConstraint(
            "order_created_at IS NULL OR order_created_at >= order_creation_started_at",
            name="order_created_after_start",
        ),
        CheckConstraint(
            "paid_at IS NULL OR paid_at >= order_creation_started_at",
            name="paid_after_order_start",
        ),
        CheckConstraint("order_creation_attempts >= 1", name="order_creation_attempts_positive"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("payment_binding_version = '1'", name="binding_version"),
        CheckConstraint("updated_at >= created_at", name="updated_at_valid"),
        _hash_shape_constraint("authorization_hash"),
        _hash_shape_constraint("policy_hash"),
        _hash_shape_constraint("quote_hash"),
        _hash_shape_constraint("payment_binding_hash"),
        CheckConstraint(
            "id ~ '^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "account_id ~ '^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="account_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "authorization_id ~ '^aut_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="authorization_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "evaluation_id ~ '^pye_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="evaluation_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "policy_id ~ '^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="policy_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "quote_id ~ '^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="quote_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "merchant_id ~ '^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="merchant_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "service_id ~ '^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="service_id_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="currency_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "provider_order_id IS NULL OR provider_order_id ~ '^order_[A-Za-z0-9]+$'",
            name="provider_order_id_format",
        ).ddl_if(dialect="postgresql"),
        _hash_format_constraint("authorization_hash"),
        _hash_format_constraint("policy_hash"),
        _hash_format_constraint("quote_hash"),
        _hash_format_constraint("payment_binding_hash"),
        Index(
            "ix_payment_transactions_account_created_at",
            "account_id",
            "created_at",
        ),
        Index(
            "ix_payment_transactions_state_updated_at",
            "transaction_state",
            "updated_at",
        ),
        Index(
            "ix_payment_transactions_merchant_created_at",
            "merchant_id",
            "created_at",
        ),
        Index(
            "ix_payment_transactions_quote_created_at",
            "quote_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(30),
        primary_key=True,
        default=new_payment_transaction_id,
    )
    account_id: Mapped[str] = mapped_column(
        String(31),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    authorization_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("purchase_authorizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    authorization_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    evaluation_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("policy_evaluations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    policy_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("buyer_policies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    policy_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    quote_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("quotes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quote_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    merchant_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    service_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("services.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    purchase_type: Mapped[PurchaseType] = mapped_column(
        Enum(
            PurchaseType,
            name="purchase_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        Enum(
            PaymentProvider,
            name="payment_provider",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=PaymentProvider.RAZORPAY,
        server_default=PaymentProvider.RAZORPAY.value,
    )
    provider_receipt: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_order_status: Mapped[RazorpayOrderStatus | None] = mapped_column(
        Enum(
            RazorpayOrderStatus,
            name="razorpay_order_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=True,
    )
    transaction_state: Mapped[PaymentTransactionState] = mapped_column(
        Enum(
            PaymentTransactionState,
            name="payment_transaction_state",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        default=PaymentTransactionState.ORDER_CREATION_PENDING,
        server_default=PaymentTransactionState.ORDER_CREATION_PENDING.value,
    )
    order_creation_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    order_creation_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    order_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    payment_binding_version: Mapped[str] = mapped_column(String(16), nullable=False)
    payment_binding_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )


@event.listens_for(PaymentTransaction, "before_insert")
def _validate_payment_transaction_insert(
    _mapper: object,
    _connection: object,
    transaction: PaymentTransaction,
) -> None:
    if transaction.provider_receipt != transaction.id:
        raise InvalidRequestError("Payment provider receipt must equal the transaction ID")
    if recompute_payment_binding_hash(transaction) != transaction.payment_binding_hash:
        raise InvalidRequestError("Payment transaction binding hash does not match its fields")


@event.listens_for(PaymentTransaction, "before_update")
def _guard_payment_transaction_update(
    _mapper: object,
    _connection: object,
    transaction: PaymentTransaction,
) -> None:
    state = sqlalchemy_inspect(transaction)
    immutable_fields = (
        "id",
        "account_id",
        "authorization_id",
        "authorization_hash",
        "evaluation_id",
        "policy_id",
        "policy_hash",
        "quote_id",
        "quote_hash",
        "merchant_id",
        "service_id",
        "amount",
        "currency",
        "purchase_type",
        "provider",
        "provider_receipt",
        "payment_binding_version",
        "payment_binding_hash",
        "created_at",
    )
    if any(state.attrs[field].history.has_changes() for field in immutable_fields):
        raise InvalidRequestError("Payment transaction binding fields are immutable")

    revision_history = state.attrs.revision.history
    if not revision_history.has_changes() or not revision_history.deleted:
        raise InvalidRequestError("Payment transaction revision must advance exactly once")
    if transaction.revision != revision_history.deleted[0] + 1:
        raise InvalidRequestError("Payment transaction revision must advance exactly once")

    state_history = state.attrs.transaction_state.history
    if state_history.has_changes() and state_history.deleted:
        if not can_transition_transaction(
            state_history.deleted[0],
            transaction.transaction_state,
        ):
            raise InvalidRequestError("Payment transaction state transition is not allowed")

    order_id_history = state.attrs.provider_order_id.history
    if order_id_history.has_changes() and order_id_history.deleted:
        prior_order_id = order_id_history.deleted[0]
        if prior_order_id is not None:
            raise InvalidRequestError("Payment provider order binding is immutable once set")

    order_status_history = state.attrs.provider_order_status.history
    if order_status_history.has_changes() and order_status_history.deleted:
        if not can_transition_order_status(
            order_status_history.deleted[0],
            transaction.provider_order_status,
        ):
            raise InvalidRequestError("Payment provider order status cannot regress")

    attempts_history = state.attrs.order_creation_attempts.history
    if attempts_history.has_changes() and attempts_history.deleted:
        if transaction.order_creation_attempts < attempts_history.deleted[0]:
            raise InvalidRequestError("Payment order creation attempts cannot regress")

    for field in (
        "order_creation_started_at",
        "order_created_at",
        "paid_at",
        "last_reconciled_at",
        "updated_at",
    ):
        history = state.attrs[field].history
        if not history.has_changes() or not history.deleted:
            continue
        prior_value = history.deleted[0]
        next_value = getattr(transaction, field)
        if prior_value is not None and (next_value is None or next_value < prior_value):
            raise InvalidRequestError(f"Payment transaction {field} cannot regress")
        if field in {"order_created_at", "paid_at"} and prior_value is not None:
            if next_value != prior_value:
                raise InvalidRequestError(f"Payment transaction {field} is immutable once recorded")


@event.listens_for(PaymentTransaction, "before_delete")
def _reject_payment_transaction_delete(*_: object) -> None:
    raise InvalidRequestError("Payment transactions cannot be deleted")
