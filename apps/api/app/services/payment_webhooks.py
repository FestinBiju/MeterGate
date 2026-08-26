"""Fast, byte-exact admission for Razorpay webhook deliveries."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

from app.cache.payment_webhooks import (
    VerifiedWebhookPayload,
    WebhookQueueDurabilityError,
    WebhookQueuePublisher,
    WebhookQueueUnavailableError,
)
from app.domain.exceptions import PaymentUnavailableError, PaymentVerificationError
from app.providers import validate_provider_event_type, verify_webhook_signature

Clock = Callable[[], datetime]
VerifiedWebhookHook = Callable[[VerifiedWebhookPayload], Awaitable[None]]
_VALUE_REVOKING_WEBHOOK_EVENTS = frozenset(
    {"refund.created", "refund.processed", "refund.speed_changed"}
)
_VALUE_REVOKING_WEBHOOK_MAXIMUM_AGE = timedelta(days=15)
_MAXIMUM_FUTURE_SKEW = timedelta(seconds=60)


def utc_now() -> datetime:
    return datetime.now(UTC)


class RazorpayWebhookIngressService:
    """Authenticate, replay-check, and durably queue a webhook without database work."""

    def __init__(
        self,
        queue: WebhookQueuePublisher | None,
        *,
        webhook_secret: str | None,
        previous_webhook_secret: str | None = None,
        maximum_age: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        if maximum_age <= timedelta(0):
            raise ValueError("Webhook maximum age must be positive")
        self._queue = queue
        self._webhook_secrets = tuple(
            secret for secret in (webhook_secret, previous_webhook_secret) if secret is not None
        )
        if len(self._webhook_secrets) != len(set(self._webhook_secrets)):
            raise ValueError("Webhook rotation secrets must be distinct")
        self._maximum_age = maximum_age
        self._clock = clock

    async def accept(
        self,
        *,
        raw_body: bytes,
        signature: str | None,
        provider_event_id: str | None,
        before_enqueue: VerifiedWebhookHook | None = None,
    ) -> None:
        if self._queue is None or not self._webhook_secrets:
            raise PaymentUnavailableError(
                "Razorpay Test Mode webhook processing is disabled",
                "PAYMENT_DISABLED",
            )
        if not signature or not verify_webhook_signature(
            raw_body=raw_body,
            signature=signature.lower(),
            webhook_secrets=self._webhook_secrets,
        ):
            raise PaymentVerificationError(
                "Razorpay webhook signature is invalid",
                "PAYMENT_WEBHOOK_SIGNATURE_INVALID",
            )
        now = self._read_clock()
        event_type, provider_created_at = self._provider_metadata(raw_body)
        age = now - provider_created_at
        maximum_age = (
            _VALUE_REVOKING_WEBHOOK_MAXIMUM_AGE
            if event_type in _VALUE_REVOKING_WEBHOOK_EVENTS
            else self._maximum_age
        )
        if age > maximum_age or age < -_MAXIMUM_FUTURE_SKEW:
            raise PaymentVerificationError(
                "Razorpay webhook event is outside the accepted replay window",
                "PAYMENT_WEBHOOK_EVENT_STALE",
            )
        try:
            payload = VerifiedWebhookPayload(
                provider_event_id=provider_event_id or "",
                received_at=now,
                raw_body=raw_body,
            )
        except ValueError as error:
            raise PaymentVerificationError(
                "Razorpay webhook event ID or body is invalid",
                "PAYMENT_WEBHOOK_EVENT_INVALID",
            ) from error
        if before_enqueue is not None:
            await before_enqueue(payload)
        try:
            await self._queue.enqueue(payload)
        except (WebhookQueueUnavailableError, WebhookQueueDurabilityError) as error:
            raise PaymentUnavailableError(
                "Verified Razorpay webhook could not be durably queued",
                "PAYMENT_WEBHOOK_QUEUE_FAILED",
            ) from error

    @staticmethod
    def _provider_metadata(raw_body: bytes) -> tuple[str, datetime]:
        try:
            value = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise PaymentVerificationError(
                "Razorpay webhook body is not valid JSON",
                "PAYMENT_WEBHOOK_PAYLOAD_INVALID",
            ) from error
        if not isinstance(value, Mapping) or value.get("entity") != "event":
            raise PaymentVerificationError(
                "Razorpay webhook envelope is invalid",
                "PAYMENT_WEBHOOK_PAYLOAD_INVALID",
            )
        timestamp = value.get("created_at")
        event_type = value.get("event")
        try:
            validate_provider_event_type(event_type)  # type: ignore[arg-type]
        except ValueError as error:
            raise PaymentVerificationError(
                "Razorpay webhook metadata is invalid",
                "PAYMENT_WEBHOOK_PAYLOAD_INVALID",
            ) from error
        if type(timestamp) is not int or not 0 <= timestamp <= (1 << 53) - 1:
            raise PaymentVerificationError(
                "Razorpay webhook metadata is invalid",
                "PAYMENT_WEBHOOK_PAYLOAD_INVALID",
            )
        try:
            provider_created_at = datetime.fromtimestamp(timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError) as error:
            raise PaymentVerificationError(
                "Razorpay webhook timestamp is invalid",
                "PAYMENT_WEBHOOK_PAYLOAD_INVALID",
            ) from error
        assert isinstance(event_type, str)
        return event_type, provider_created_at

    def _read_clock(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Webhook clock must return a timezone-aware datetime")
        return value.astimezone(UTC)
