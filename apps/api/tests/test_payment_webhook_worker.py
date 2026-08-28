from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.cache.payment_webhooks import (
    ClaimedWebhookBatch,
    QueuedWebhookMessage,
    VerifiedWebhookPayload,
)
from app.workers.razorpay_webhooks import RazorpayWebhookWorker, run_worker_cli

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def message(stream_id: str, event_id: str) -> QueuedWebhookMessage:
    return QueuedWebhookMessage(
        stream_id=stream_id,
        payload=VerifiedWebhookPayload(
            provider_event_id=event_id,
            received_at=NOW,
            raw_body=b'{"sensitive":"raw-provider-body"}',
        ),
    )


class FakeQueue:
    def __init__(self) -> None:
        self.initialized = False
        self.stale: tuple[QueuedWebhookMessage, ...] = ()
        self.new: tuple[QueuedWebhookMessage, ...] = ()
        self.claim_calls: list[tuple[object, ...]] = []
        self.read_calls: list[tuple[object, ...]] = []
        self.acks: list[str] = []
        self.ack_result = 1

    async def initialize(self) -> None:
        self.initialized = True

    async def claim_stale(
        self,
        consumer_name: str,
        *,
        minimum_idle_ms: int,
        start_id: str = "0-0",
        count: int = 10,
    ) -> ClaimedWebhookBatch:
        self.claim_calls.append((consumer_name, minimum_idle_ms, start_id, count))
        return ClaimedWebhookBatch(next_start_id="0-0", messages=self.stale)

    async def read_new(
        self,
        consumer_name: str,
        *,
        count: int = 10,
        block_ms: int = 5_000,
    ) -> tuple[QueuedWebhookMessage, ...]:
        self.read_calls.append((consumer_name, count, block_ms))
        return self.new

    async def acknowledge_and_delete(self, message_ids: list[str]) -> int:
        self.acks.extend(message_ids)
        return self.ack_result


class FakeProcessor:
    def __init__(self, *, fail_event_ids: set[str] | None = None) -> None:
        self.fail_event_ids = fail_event_ids or set()
        self.processed: list[str] = []

    async def process_webhook(self, payload: VerifiedWebhookPayload) -> None:
        self.processed.append(payload.provider_event_id)
        if payload.provider_event_id in self.fail_event_ids:
            raise RuntimeError("provider body detail must not be logged")


@pytest.mark.asyncio
async def test_worker_reclaims_before_new_and_acks_only_successful_processing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = FakeQueue()
    queue.stale = (message("100-0", "event-old"),)
    queue.new = (message("101-0", "event-new"),)
    processor = FakeProcessor(fail_event_ids={"event-new"})
    worker = RazorpayWebhookWorker(
        queue=queue,
        processor=processor,
        consumer_name="worker-1",
        batch_size=3,
        block_ms=10,
    )

    result = await worker.run_once()

    assert processor.processed == ["event-old", "event-new"]
    assert queue.acks == ["100-0"]
    assert result.delivered == 2
    assert result.processed == 1
    assert result.failed == 1
    assert queue.claim_calls == [("worker-1", 30_000, "0-0", 3)]
    assert queue.read_calls == [("worker-1", 2, 10)]
    assert "raw-provider-body" not in caplog.text
    assert "provider body detail" not in caplog.text


@pytest.mark.asyncio
async def test_worker_leaves_message_pending_when_acknowledgement_is_ambiguous() -> None:
    queue = FakeQueue()
    queue.new = (message("100-0", "event-1"),)
    queue.ack_result = 0
    processor = FakeProcessor()
    worker = RazorpayWebhookWorker(
        queue=queue,
        processor=processor,
        consumer_name="worker-1",
    )

    result = await worker.run_once()

    assert processor.processed == ["event-1"]
    assert result.failed == 1


@pytest.mark.asyncio
async def test_cli_enters_runtime_initializes_worker_and_closes_cleanly() -> None:
    queue = FakeQueue()
    processor = FakeProcessor()
    worker = RazorpayWebhookWorker(
        queue=queue,
        processor=processor,
        consumer_name="worker-1",
    )
    exited = False

    @asynccontextmanager
    async def runtime() -> AsyncIterator[RazorpayWebhookWorker]:
        nonlocal exited
        try:
            yield worker
        finally:
            exited = True

    import asyncio

    stop_event = asyncio.Event()
    stop_event.set()
    await run_worker_cli(runtime_factory=runtime, stop_event=stop_event)

    assert queue.initialized
    assert exited
