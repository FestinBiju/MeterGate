"""Stable persisted values for the initial merchant catalog domain."""

from enum import StrEnum


class AccountStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


class ApprovalIdentityStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class MerchantStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class ServiceStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class ServiceType(StrEnum):
    API = "api"
    REPORT = "report"
    DATASET = "dataset"
    INFERENCE = "inference"
    DIGITAL_ASSET = "digital_asset"
    OTHER = "other"


class PurchaseType(StrEnum):
    ONE_TIME = "one_time"
    SUBSCRIPTION = "subscription"
    USAGE_BASED = "usage_based"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class PaymentProvider(StrEnum):
    RAZORPAY = "razorpay"


class PaymentTransactionState(StrEnum):
    ORDER_CREATION_PENDING = "order_creation_pending"
    ORDER_CREATED = "order_created"
    ORDER_CREATION_FAILED = "order_creation_failed"
    ORDER_CREATION_UNCERTAIN = "order_creation_uncertain"
    PAYMENT_PENDING = "payment_pending"
    PAYMENT_AUTHORIZED = "payment_authorized"
    PAID = "paid"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class RazorpayOrderStatus(StrEnum):
    CREATED = "created"
    ATTEMPTED = "attempted"
    PAID = "paid"


class PaymentAttemptStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class WebhookProcessingStatus(StrEnum):
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class PaymentEventActorType(StrEnum):
    ACCOUNT = "account"
    SYSTEM = "system"
    PROVIDER_API = "provider_api"
    PROVIDER_WEBHOOK = "provider_webhook"


class PaymentTransactionEventType(StrEnum):
    PAYMENT_TRANSACTION_CREATED = "payment_transaction_created"
    RAZORPAY_ORDER_CREATION_STARTED = "razorpay_order_creation_started"
    RAZORPAY_ORDER_CREATED = "razorpay_order_created"
    RAZORPAY_ORDER_CREATION_FAILED = "razorpay_order_creation_failed"
    RAZORPAY_ORDER_CREATION_UNCERTAIN = "razorpay_order_creation_uncertain"
    CHECKOUT_SIGNATURE_VERIFIED = "checkout_signature_verified"
    PAYMENT_AUTHORIZED = "payment_authorized"
    PAYMENT_ATTEMPT_FAILED = "payment_attempt_failed"
    PAYMENT_CAPTURED = "payment_captured"
    ORDER_PAID = "order_paid"
    PAYMENT_RECONCILED = "payment_reconciled"
    RECONCILIATION_REQUIRED = "reconciliation_required"
