"""Persistence repositories for canonical domain records."""

from app.repositories.accounts import AccountRepository
from app.repositories.approval_identities import ApprovalIdentityRepository
from app.repositories.authorizations import PurchaseAuthorizationRepository
from app.repositories.buyer_policies import BuyerPolicyRepository
from app.repositories.commerce_outbox_events import CommerceOutboxEventRepository
from app.repositories.compensation_cases import CompensationCaseRepository
from app.repositories.compensation_events import CompensationEventRepository
from app.repositories.entitlements import EntitlementRepository
from app.repositories.fulfillment_events import FulfillmentEventRepository
from app.repositories.fulfillment_executions import FulfillmentExecutionRepository
from app.repositories.merchants import MerchantRepository
from app.repositories.passkey_credentials import PasskeyCredentialRepository
from app.repositories.payment_attempts import PaymentAttemptRepository
from app.repositories.payment_refunds import PaymentRefundRepository
from app.repositories.payment_transaction_events import PaymentTransactionEventRepository
from app.repositories.payment_transactions import PaymentTransactionRepository
from app.repositories.policy_evaluations import PolicyEvaluationRepository
from app.repositories.quotes import QuoteRepository
from app.repositories.razorpay_webhook_events import RazorpayWebhookEventRepository
from app.repositories.refund_outbox_events import RefundOutboxEventRepository
from app.repositories.service_fulfillment_configs import ServiceFulfillmentConfigRepository
from app.repositories.services import ServiceRepository

__all__ = [
    "AccountRepository",
    "ApprovalIdentityRepository",
    "BuyerPolicyRepository",
    "CommerceOutboxEventRepository",
    "CompensationCaseRepository",
    "CompensationEventRepository",
    "EntitlementRepository",
    "FulfillmentEventRepository",
    "FulfillmentExecutionRepository",
    "MerchantRepository",
    "PasskeyCredentialRepository",
    "PaymentAttemptRepository",
    "PaymentRefundRepository",
    "PaymentTransactionEventRepository",
    "PaymentTransactionRepository",
    "PolicyEvaluationRepository",
    "PurchaseAuthorizationRepository",
    "QuoteRepository",
    "RazorpayWebhookEventRepository",
    "RefundOutboxEventRepository",
    "ServiceRepository",
    "ServiceFulfillmentConfigRepository",
]
