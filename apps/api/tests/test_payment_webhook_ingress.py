from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.v1.dependencies import (
    get_application_settings,
    get_payment_webhook_queue,
    require_authenticated_mutation,
    require_current_account,
)
from app.cache.payment_webhooks import (
    VerifiedWebhookPayload,
    WebhookQueueDurabilityError,
    WebhookQueueUnavailableError,
)
from app.core.config import Settings
from app.domain.exceptions import PaymentVerificationError
from app.providers import webhook_signature_digest
from app.services.payment_webhooks import RazorpayWebhookIngressService

WEBHOOK_SECRET = "test-webhook-secret"
PREVIOUS_WEBHOOK_SECRET = "previous-webhook-secret"
EVENT_ID = "evt_test_delivery_001"
ENDPOINT = "/api/v1/webhooks/razorpay"
FIXED_NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


class RecordingQueue:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.payloads: list[VerifiedWebhookPayload] = []

    async def enqueue(self, payload: VerifiedWebhookPayload) -> str:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return "1-0"


def webhook_body(
    *,
    created_at: datetime | None = None,
    amount: int = 500,
    event_type: str = "payment.captured",
) -> bytes:
    event_time = created_at or datetime.now(UTC)
    return json.dumps(
        {
            "entity": "event",
            "event": event_type,
            "created_at": int(event_time.timestamp()),
            "payload": {"payment": {"entity": {"amount": amount}}},
        },
        separators=(",", ":"),
    ).encode("utf-8")


def signed_headers(
    body: bytes,
    *,
    include_signature: bool = True,
    include_event_id: bool = True,
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if include_signature:
        headers["X-Razorpay-Signature"] = webhook_signature_digest(
            raw_body=body,
            webhook_secret=WEBHOOK_SECRET,
        )
    if include_event_id:
        headers["X-Razorpay-Event-Id"] = EVENT_ID
    return headers


def client_with_webhook_ingress(
    make_client: Callable[..., TestClient],
    settings: Settings,
    queue: RecordingQueue | None = None,
    *,
    maximum_body_bytes: int = 262_144,
) -> tuple[TestClient, RecordingQueue]:
    client = make_client()
    recording_queue = queue or RecordingQueue()
    webhook_settings = settings.model_copy(
        update={
            "payments_enabled": True,
            "razorpay_webhook_secret": SecretStr(WEBHOOK_SECRET),
            "razorpay_webhook_max_age_seconds": 300,
            "razorpay_webhook_max_body_bytes": maximum_body_bytes,
        }
    )
    client.app.dependency_overrides[get_application_settings] = lambda: webhook_settings
    client.app.dependency_overrides[get_payment_webhook_queue] = lambda: recording_queue
    return client, recording_queue


def assert_payment_error(response: Any, status_code: int, reason_code: str) -> None:
    assert response.status_code == status_code
    body = response.json()
    assert body["reason_code"] == reason_code
    assert set(body) == {"detail", "reason_code"}


def test_public_webhook_needs_no_cookie_origin_csrf_or_account_dependency(
    make_client: Callable[..., TestClient],
    settings: Settings,
) -> None:
    client, queue = client_with_webhook_ingress(make_client, settings)

    def protected_dependency_must_not_run() -> None:
        raise AssertionError("public webhook invoked an account dependency")

    client.app.dependency_overrides[require_current_account] = protected_dependency_must_not_run
    client.app.dependency_overrides[require_authenticated_mutation] = (
        protected_dependency_must_not_run
    )
    body = webhook_body()

    response = client.post(ENDPOINT, content=body, headers=signed_headers(body))

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "reason_code": "PAYMENT_WEBHOOK_ACCEPTED",
    }
    assert "set-cookie" not in response.headers
    assert len(queue.payloads) == 1
    assert queue.payloads[0].provider_event_id == EVENT_ID
    assert queue.payloads[0].raw_body == body


def test_signature_is_checked_over_raw_bytes_before_json_parsing(
    make_client: Callable[..., TestClient],
    settings: Settings,
) -> None:
    client, queue = client_with_webhook_ingress(make_client, settings)
    invalid_json = b'{"entity":"event","created_at":'
    invalid_signature_headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": "0" * 64,
        "X-Razorpay-Event-Id": EVENT_ID,
    }

    unauthenticated = client.post(
        ENDPOINT,
        content=invalid_json,
        headers=invalid_signature_headers,
    )
    authenticated = client.post(
        ENDPOINT,
        content=invalid_json,
        headers=signed_headers(invalid_json),
    )

    assert_payment_error(unauthenticated, 400, "PAYMENT_WEBHOOK_SIGNATURE_INVALID")
    assert_payment_error(authenticated, 400, "PAYMENT_WEBHOOK_PAYLOAD_INVALID")
    assert queue.payloads == []


def test_any_change_to_the_signed_body_is_rejected(
    make_client: Callable[..., TestClient],
    settings: Settings,
) -> None:
    client, queue = client_with_webhook_ingress(make_client, settings)
    original = webhook_body(amount=500)
    changed = original.replace(b'"amount":500', b'"amount":501')
    headers = signed_headers(original)

    response = client.post(ENDPOINT, content=changed, headers=headers)

    assert changed != original
    assert_payment_error(response, 400, "PAYMENT_WEBHOOK_SIGNATURE_INVALID")
    assert queue.payloads == []


@pytest.mark.parametrize(
    ("header_kind", "reason_code"),
    [
        ("missing_signature", "PAYMENT_WEBHOOK_SIGNATURE_INVALID"),
        ("missing_event_id", "PAYMENT_WEBHOOK_EVENT_INVALID"),
        ("duplicate_signature", "PAYMENT_WEBHOOK_SIGNATURE_INVALID"),
        ("duplicate_event_id", "PAYMENT_WEBHOOK_EVENT_INVALID"),
    ],
)
def test_missing_or_duplicate_evidence_headers_fail_closed(
    make_client: Callable[..., TestClient],
    settings: Settings,
    header_kind: str,
    reason_code: str,
) -> None:
    client, queue = client_with_webhook_ingress(make_client, settings)
    body = webhook_body()
    signature = webhook_signature_digest(
        raw_body=body,
        webhook_secret=WEBHOOK_SECRET,
    )
    headers: list[tuple[str, str]] = [("Content-Type", "application/json")]
    if header_kind != "missing_signature":
        headers.append(("X-Razorpay-Signature", signature))
    if header_kind == "duplicate_signature":
        headers.append(("X-Razorpay-Signature", signature))
    if header_kind != "missing_event_id":
        headers.append(("X-Razorpay-Event-Id", EVENT_ID))
    if header_kind == "duplicate_event_id":
        headers.append(("X-Razorpay-Event-Id", EVENT_ID))

    response = client.post(ENDPOINT, content=body, headers=headers)

    assert_payment_error(response, 400, reason_code)
    assert queue.payloads == []


@pytest.mark.asyncio
async def test_injected_clock_rejects_signed_stale_event_before_enqueue() -> None:
    queue = RecordingQueue()
    ingress = RazorpayWebhookIngressService(
        queue,  # type: ignore[arg-type]
        webhook_secret=WEBHOOK_SECRET,
        maximum_age=timedelta(seconds=300),
        clock=lambda: FIXED_NOW,
    )
    body = webhook_body(created_at=FIXED_NOW - timedelta(seconds=301))

    with pytest.raises(PaymentVerificationError) as error_info:
        await ingress.accept(
            raw_body=body,
            signature=signed_headers(body)["X-Razorpay-Signature"],
            provider_event_id=EVENT_ID,
        )

    assert error_info.value.reason_code == "PAYMENT_WEBHOOK_EVENT_STALE"
    assert queue.payloads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    ["refund.created", "refund.processed", "refund.speed_changed"],
)
async def test_signed_value_revoking_refund_accepts_dashboard_replay_window(
    event_type: str,
) -> None:
    queue = RecordingQueue()
    ingress = RazorpayWebhookIngressService(
        queue,  # type: ignore[arg-type]
        webhook_secret=WEBHOOK_SECRET,
        maximum_age=timedelta(seconds=300),
        clock=lambda: FIXED_NOW,
    )
    body = webhook_body(
        created_at=FIXED_NOW - timedelta(days=14, hours=23),
        event_type=event_type,
    )

    await ingress.accept(
        raw_body=body,
        signature=signed_headers(body)["X-Razorpay-Signature"],
        provider_event_id=EVENT_ID,
    )

    assert len(queue.payloads) == 1
    assert queue.payloads[0].raw_body == body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "created_at"),
    [
        ("refund.processed", FIXED_NOW - timedelta(days=15, seconds=1)),
        ("refund.failed", FIXED_NOW - timedelta(seconds=301)),
        ("refund.created", FIXED_NOW + timedelta(seconds=61)),
    ],
)
async def test_refund_replay_window_rejects_expired_or_future_events(
    event_type: str,
    created_at: datetime,
) -> None:
    queue = RecordingQueue()
    ingress = RazorpayWebhookIngressService(
        queue,  # type: ignore[arg-type]
        webhook_secret=WEBHOOK_SECRET,
        maximum_age=timedelta(seconds=300),
        clock=lambda: FIXED_NOW,
    )
    body = webhook_body(created_at=created_at, event_type=event_type)

    with pytest.raises(PaymentVerificationError) as error_info:
        await ingress.accept(
            raw_body=body,
            signature=signed_headers(body)["X-Razorpay-Signature"],
            provider_event_id=EVENT_ID,
        )

    assert error_info.value.reason_code == "PAYMENT_WEBHOOK_EVENT_STALE"
    assert queue.payloads == []


@pytest.mark.asyncio
async def test_webhook_rotation_accepts_previous_secret_during_overlap() -> None:
    queue = RecordingQueue()
    ingress = RazorpayWebhookIngressService(
        queue,  # type: ignore[arg-type]
        webhook_secret=WEBHOOK_SECRET,
        previous_webhook_secret=PREVIOUS_WEBHOOK_SECRET,
        maximum_age=timedelta(seconds=300),
        clock=lambda: FIXED_NOW,
    )
    body = webhook_body(created_at=FIXED_NOW)
    signature = webhook_signature_digest(
        raw_body=body,
        webhook_secret=PREVIOUS_WEBHOOK_SECRET,
    )

    await ingress.accept(
        raw_body=body,
        signature=signature,
        provider_event_id=EVENT_ID,
    )

    assert len(queue.payloads) == 1
    assert queue.payloads[0].raw_body == body


@pytest.mark.asyncio
async def test_verified_hook_runs_before_durable_queue_admission() -> None:
    order: list[str] = []

    class OrderedQueue(RecordingQueue):
        async def enqueue(self, payload: VerifiedWebhookPayload) -> str:
            assert order == ["quarantined"]
            order.append("enqueued")
            return await super().enqueue(payload)

    async def quarantine(_payload: VerifiedWebhookPayload) -> None:
        order.append("quarantined")

    queue = OrderedQueue()
    ingress = RazorpayWebhookIngressService(
        queue,  # type: ignore[arg-type]
        webhook_secret=WEBHOOK_SECRET,
        maximum_age=timedelta(seconds=300),
        clock=lambda: FIXED_NOW,
    )
    body = webhook_body(created_at=FIXED_NOW)

    await ingress.accept(
        raw_body=body,
        signature=signed_headers(body)["X-Razorpay-Signature"],
        provider_event_id=EVENT_ID,
        before_enqueue=quarantine,
    )

    assert order == ["quarantined", "enqueued"]


def test_disabled_payments_rejects_webhook_with_injected_secret_and_queue(
    make_client: Callable[..., TestClient],
    settings: Settings,
) -> None:
    client = make_client()
    queue = RecordingQueue()
    disabled_settings = settings.model_copy(
        update={
            "payments_enabled": False,
            "razorpay_webhook_secret": SecretStr(WEBHOOK_SECRET),
        }
    )
    client.app.dependency_overrides[get_application_settings] = lambda: disabled_settings
    client.app.dependency_overrides[get_payment_webhook_queue] = lambda: queue
    body = webhook_body()

    response = client.post(ENDPOINT, content=body, headers=signed_headers(body))

    assert_payment_error(response, 503, "PAYMENT_DISABLED")
    assert queue.payloads == []


def test_signed_invalid_event_type_is_rejected_before_enqueue(
    make_client: Callable[..., TestClient],
    settings: Settings,
) -> None:
    client, queue = client_with_webhook_ingress(make_client, settings)
    body = webhook_body(event_type=" payment.captured")

    response = client.post(ENDPOINT, content=body, headers=signed_headers(body))

    assert_payment_error(response, 400, "PAYMENT_WEBHOOK_PAYLOAD_INVALID")
    assert queue.payloads == []


def test_body_limit_is_enforced_before_signature_or_json_work(
    make_client: Callable[..., TestClient],
    settings: Settings,
) -> None:
    client, queue = client_with_webhook_ingress(
        make_client,
        settings,
        maximum_body_bytes=1_024,
    )
    body = b"x" * 1_025

    response = client.post(ENDPOINT, content=body, headers=signed_headers(body))
    streamed_body = b"y" * 1_200
    streamed = client.post(
        ENDPOINT,
        content=iter((streamed_body[:600], streamed_body[600:])),
        headers=signed_headers(streamed_body),
    )

    assert_payment_error(response, 400, "PAYMENT_WEBHOOK_BODY_TOO_LARGE")
    assert_payment_error(streamed, 400, "PAYMENT_WEBHOOK_BODY_TOO_LARGE")
    assert queue.payloads == []


@pytest.mark.parametrize(
    "queue_error",
    [
        WebhookQueueUnavailableError("redis unavailable: private-host"),
        WebhookQueueDurabilityError("AOF confirmation failed: private-detail"),
    ],
)
def test_verified_webhook_queue_failures_are_sanitized_and_retryable(
    make_client: Callable[..., TestClient],
    settings: Settings,
    queue_error: Exception,
) -> None:
    queue = RecordingQueue(queue_error)
    client, queue = client_with_webhook_ingress(make_client, settings, queue)
    body = webhook_body()

    response = client.post(ENDPOINT, content=body, headers=signed_headers(body))

    assert_payment_error(response, 503, "PAYMENT_WEBHOOK_QUEUE_FAILED")
    assert response.json()["detail"] == "Verified Razorpay webhook could not be durably queued"
    assert "private" not in response.text
    assert len(queue.payloads) == 1
    assert queue.payloads[0].raw_body == body
