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


class CompensationDecisionState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    MANUAL_REVIEW = "manual_review"


class CompensationRecommendedAction(StrEnum):
    FULL_REFUND = "full_refund"
    MANUAL_REVIEW = "manual_review"
    NO_REFUND = "no_refund"


class CompensationDecisionProvenance(StrEnum):
    AUTOMATIC_APPROVED = "automatic_approved"
    MANUAL_APPROVED = "manual_approved"
    MANUAL_REJECTED = "manual_rejected"
    MANUAL_REVIEW = "manual_review"


class PaymentRefundState(StrEnum):
    REFUND_PENDING = "refund_pending"
    REFUND_PROCESSING = "refund_processing"
    REFUNDED = "refunded"
    REFUND_FAILED = "refund_failed"
    REFUND_UNCERTAIN = "refund_uncertain"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class CompensationEventType(StrEnum):
    COMPENSATION_CASE_CREATED = "compensation_case_created"
    COMPENSATION_RECOMMENDED = "compensation_recommended"
    COMPENSATION_APPROVED = "compensation_approved"
    COMPENSATION_REJECTED = "compensation_rejected"
    REFUND_OUTBOX_CREATED = "refund_outbox_created"
    REFUND_REQUESTED = "refund_requested"
    REFUND_REQUEST_STARTED = "refund_request_started"
    RAZORPAY_REFUND_CREATED = "razorpay_refund_created"
    REFUND_PROCESSING = "refund_processing"
    REFUND_COMPLETED = "refund_completed"
    REFUND_FAILED = "refund_failed"
    REFUND_UNCERTAIN = "refund_uncertain"
    REFUND_RECONCILIATION_REQUIRED = "refund_reconciliation_required"
    REFUND_RECONCILIATION_RESOLVED = "refund_reconciliation_resolved"
    COMPENSATION_CLOSED = "compensation_closed"


class CompensationEventActorType(StrEnum):
    SYSTEM = "system"
    REFUND_WORKER = "refund_worker"
    PROVIDER_API = "provider_api"
    PROVIDER_WEBHOOK = "provider_webhook"
    OPERATOR = "operator"


class RefundOutboxEventType(StrEnum):
    REFUND_REQUESTED = "refund_requested"


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
    PAYMENT_REVERIFIED = "payment_reverified"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class CommerceOutboxEventType(StrEnum):
    ENTITLEMENT_ISSUANCE_REQUESTED = "entitlement_issuance_requested"


class CommerceAggregateType(StrEnum):
    PAYMENT_TRANSACTION = "payment_transaction"


class FulfillmentExecutionState(StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    RETRYABLE_FAILURE = "retryable_failure"
    SUCCEEDED = "succeeded"
    PERMANENT_FAILURE = "permanent_failure"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class FulfillmentEventType(StrEnum):
    PAYMENT_REVERIFIED = "payment_reverified"
    ENTITLEMENT_ISSUED = "entitlement_issued"
    CAPABILITY_ISSUED = "capability_issued"
    FULFILLMENT_CLAIMED = "fulfillment_claimed"
    FULFILLMENT_STARTED = "fulfillment_started"
    MERCHANT_REQUEST_SENT = "merchant_request_sent"
    MERCHANT_RESPONSE_RECEIVED = "merchant_response_received"
    FULFILLMENT_SUCCEEDED = "fulfillment_succeeded"
    FULFILLMENT_RETRY_SCHEDULED = "fulfillment_retry_scheduled"
    FULFILLMENT_FAILED = "fulfillment_failed"
    COMPENSATION_REQUIRED = "compensation_required"


class FulfillmentEventActorType(StrEnum):
    ACCOUNT = "account"
    SYSTEM = "system"
    ENTITLEMENT_WORKER = "entitlement_worker"
    RESOURCE_GATEWAY = "resource_gateway"
    MERCHANT_SERVICE = "merchant_service"


class FulfillmentProviderType(StrEnum):
    HTTP = "http"
