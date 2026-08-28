"""Consumer-group worker for durable, authenticated Razorpay webhook messages."""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import signal
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.cache.payment_webhooks import (
    QueuedWebhookMessage,
    VerifiedWebhookPayload,
    WebhookQueueConsumer,
)

_CONSUMER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")

WebhookWorkerRuntimeFactory = Callable[
    [],
    AbstractAsyncContextManager["RazorpayWebhookWorker"],
]
HeartbeatWriter = Callable[[bool], Awaitable[None]]


@runtime_checkable
class WebhookEventProcessor(Protocol):
    """Database-backed idempotent event processor supplied by the application layer."""

    async def process_webhook(self, payload: VerifiedWebhookPayload) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerCycleResult:
    delivered: int
    processed: int
    failed: int

    def __post_init__(self) -> None:
        if (
            type(self.delivered) is not int
            or type(self.processed) is not int
            or type(self.failed) is not int
            or min(self.delivered, self.processed, self.failed) < 0
            or self.processed + self.failed != self.delivered
        ):
            raise ValueError("Worker cycle counts are invalid")


class RazorpayWebhookWorker:
    """At-least-once queue worker; processor transactions own effect idempotency."""

    def __init__(
        self,
        *,
        queue: WebhookQueueConsumer,
        processor: WebhookEventProcessor,
        consumer_name: str,
        batch_size: int = 10,
        block_ms: int = 5_000,
        reclaim_idle_ms: int = 30_000,
        logger: logging.Logger | None = None,
        heartbeat_writer: HeartbeatWriter | None = None,
    ) -> None:
        if (
            not isinstance(consumer_name, str)
            or _CONSUMER_NAME_PATTERN.fullmatch(consumer_name) is None
        ):
            raise ValueError("Webhook worker consumer name is invalid")
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError("Webhook worker batch size must be between one and 100")
        if type(block_ms) is not int or not 0 <= block_ms <= 60_000:
            raise ValueError("Webhook worker block time must be between zero and 60000 ms")
        if type(reclaim_idle_ms) is not int or not 1 <= reclaim_idle_ms <= 86_400_000:
            raise ValueError("Webhook worker reclaim time must be positive and bounded")
        self._queue = queue
        self._processor = processor
        self._consumer_name = consumer_name
        self._batch_size = batch_size
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._logger = logger or logging.getLogger(__name__)
        self._claim_cursor = "0-0"
        self._heartbeat_writer = heartbeat_writer

    async def initialize(self) -> None:
        await self._queue.initialize()

    async def run_once(self) -> WorkerCycleResult:
        """Reclaim abandoned deliveries, then block for remaining new capacity."""
        stale = await self._queue.claim_stale(
            self._consumer_name,
            minimum_idle_ms=self._reclaim_idle_ms,
            start_id=self._claim_cursor,
            count=self._batch_size,
        )
        self._claim_cursor = stale.next_start_id
        messages = list(stale.messages)
        remaining = self._batch_size - len(messages)
        if remaining > 0:
            messages.extend(
                await self._queue.read_new(
                    self._consumer_name,
                    count=remaining,
                    block_ms=self._block_ms,
                )
            )

        processed = 0
        for message in messages:
            if await self._process(message):
                processed += 1
        delivered = len(messages)
        return WorkerCycleResult(
            delivered=delivered,
            processed=processed,
            failed=delivered - processed,
        )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Run until asked to stop; queue failures surface to the process supervisor."""
        if not isinstance(stop_event, asyncio.Event):
            raise ValueError("Webhook worker requires an asyncio stop event")
        await self.initialize()
        while not stop_event.is_set():
            result = await self.run_once()
            if self._heartbeat_writer is not None:
                await self._heartbeat_writer(result.processed > 0)

    async def _process(self, message: QueuedWebhookMessage) -> bool:
        try:
            await self._processor.process_webhook(message.payload)
            acknowledged = await self._queue.acknowledge_and_delete([message.stream_id])
            if acknowledged != 1:
                raise RuntimeError("Webhook message was not acknowledged")
        except Exception as error:
            self._logger.warning(
                "payment_webhook_processing_failed",
                extra={
                    "provider_event_id": message.payload.provider_event_id,
                    "stream_id": message.stream_id,
                    "failure_type": type(error).__name__,
                },
            )
            return False
        return True


def _load_runtime_factory() -> WebhookWorkerRuntimeFactory:
    """Load application-owned database/Redis/service construction only for the CLI."""
    module = importlib.import_module("app.services.payments")
    factory = getattr(module, "build_razorpay_webhook_worker_runtime", None)
    if not callable(factory):
        raise RuntimeError("Payment webhook worker runtime factory is unavailable")
    return factory


async def run_worker_cli(
    *,
    runtime_factory: WebhookWorkerRuntimeFactory | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run one application-owned worker runtime with graceful signal shutdown."""
    owns_stop_event = stop_event is None
    effective_stop_event = stop_event or asyncio.Event()
    factory = runtime_factory or _load_runtime_factory()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    if owns_stop_event:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_number, effective_stop_event.set)
            except (NotImplementedError, RuntimeError):
                continue
            installed_signals.append(signal_number)

    try:
        async with factory() as worker:
            await worker.run_forever(effective_stop_event)
    finally:
        for signal_number in installed_signals:
            loop.remove_signal_handler(signal_number)


def main() -> None:
    """Synchronous module entrypoint used by ``python -m``."""
    try:
        asyncio.run(run_worker_cli())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
