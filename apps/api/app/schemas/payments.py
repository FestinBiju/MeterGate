"""Strict public contracts for the Razorpay Test Mode payment gate."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StringConstraints

from app.domain.enums import PaymentAttemptStatus, PaymentTransactionState, PurchaseType
from app.schemas.approvals import AuthorizationId, AuthorizationParty
from app.schemas.common import APIModel, CurrencyCode

PaymentTransactionId = Annotated[
    str,
    StringConstraints(pattern=r"^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
ProviderOrderId = Annotated[
    str,
    StringConstraints(pattern=r"^order_[A-Za-z0-9]{1,58}$"),
]
ProviderPaymentId = Annotated[
    str,
    StringConstraints(pattern=r"^pay_[A-Za-z0-9]{1,60}$"),
]
RazorpaySignature = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-fA-F]{64}$"),
]
PaymentAmount = Annotated[StrictInt, Field(ge=1, le=(1 << 53) - 1)]


class PaymentTransactionCreate(APIModel):
    authorization_id: AuthorizationId


class CheckoutVerificationCreate(APIModel):
    razorpay_payment_id: ProviderPaymentId
    razorpay_order_id: ProviderOrderId
    razorpay_signature: RazorpaySignature


class RazorpayCheckoutConfiguration(APIModel):
    transaction_id: PaymentTransactionId
    provider: Literal["razorpay"] = "razorpay"
    key_id: Annotated[str, StringConstraints(pattern=r"^rzp_test_[A-Za-z0-9]{8,64}$")]
    order_id: ProviderOrderId
    amount: PaymentAmount
    currency: CurrencyCode
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=255)]


class PaymentAttemptResponse(APIModel):
    id: str
    provider_payment_id: ProviderPaymentId
    status: PaymentAttemptStatus
    amount: PaymentAmount
    currency: CurrencyCode
    captured: bool
    method: str | None
    provider_created_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime


class PaymentTransactionResponse(APIModel):
    reason_code: str = Field(min_length=1, max_length=100)
    transaction_id: PaymentTransactionId
    state: PaymentTransactionState
    merchant: AuthorizationParty
    service: AuthorizationParty
    amount: PaymentAmount
    currency: CurrencyCode
    purchase_type: PurchaseType
    authorization_id: AuthorizationId
    provider: Literal["razorpay"] = "razorpay"
    provider_order_id: ProviderOrderId | None
    provider_order_status: Literal["created", "attempted", "paid"] | None
    attempts: list[PaymentAttemptResponse]
    checkout: RazorpayCheckoutConfiguration | None
    created_at: datetime
    paid_at: datetime | None
    last_reconciled_at: datetime | None


class RazorpayWebhookAccepted(APIModel):
    status: Literal["accepted", "ignored"]
    reason_code: Literal["PAYMENT_WEBHOOK_ACCEPTED", "PAYMENT_WEBHOOK_EVENT_STALE"]
