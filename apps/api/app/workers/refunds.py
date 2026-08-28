"""PostgreSQL outbox worker for idempotent Razorpay Test Mode refunds."""

from __future__ import annotations

import asyncio
import logging
import secrets
import signal
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings, get_settings
from app.db.session import Database, create_database
from app.domain.exceptions import CompensationError
from app.providers import RefundProvider
from app.repositories import RefundOutboxEventRepository
from app.services.compensations import RefundApplicationService
from app.services.payments import build_razorpay_payment_provider
from app.services.worker_health import record_worker_heartbeat

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RefundWorkerCycle:
    claimed: bool
    processed: bool
    reason_code: str | None
    refund_id: str | None = None


class RefundOutboxWorker:
    """Claim one request with SKIP LOCKED and retain lease fencing on completion."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        provider: RefundProvider | None,
        *,
        clock: Clock = utc_now,
        logger: logging.Logger | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._provider = provider
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._instance_id = f"ref-{secrets.token_urlsafe(12)}"

    async def run_once(self) -> RefundWorkerCycle:
        if not self._settings.refunds_enabled or self._provider is None:
            return RefundWorkerCycle(False, False, "REFUND_EXECUTION_DISABLED")
        now = self._read_clock()
        async with self._database.session() as session:
            outbox = RefundOutboxEventRepository(session)
            event = await outbox.claim_next(
                now=now,
                lease_expires_at=now
                + timedelta(seconds=self._settings.refund_outbox_lease_seconds),
            )
            if event is None:
                return RefundWorkerCycle(False, False, None)
            event_id = event.id
            case_id = event.compensation_case_id
            generation = event.lease_generation
            service = RefundApplicationService(
                session,
                self._provider,
                self._settings,
                clock=self._clock,
            )
            try:
                result = await service.execute_case(case_id)
            except CompensationError as error:
                await session.rollback()
                await self._release(
                    outbox,
                    event_id=event_id,
                    expected_generation=generation,
                    reason_code=error.reason_code,
                )
                self._logger.warning(
                    "refund_execution_deferred",
                    extra={
                        "refund_outbox_event_id": event_id,
                        "compensation_case_id": case_id,
                        "reason_code": error.reason_code,
                    },
                )
                return RefundWorkerCycle(True, False, error.reason_code)

            if not result.terminal:
                await self._release(
                    outbox,
                    event_id=event_id,
                    expected_generation=generation,
                    reason_code=result.reason_code,
                )
                return RefundWorkerCycle(
                    True,
                    False,
                    result.reason_code,
                    refund_id=result.refund_id,
                )
            current = await outbox.get(event_id)
            if current is None:
                await session.rollback()
                return RefundWorkerCycle(
                    True, False, "REFUND_OUTBOX_MISSING", refund_id=result.refund_id
                )
            try:
                await outbox.mark_processed(
                    current,
                    expected_lease_generation=generation,
                    processed_at=self._read_clock(),
                )
            except ValueError:
                await session.rollback()
                return RefundWorkerCycle(
                    True, False, "REFUND_WORK_LEASE_LOST", refund_id=result.refund_id
                )
            self._logger.info(
                "refund_execution_terminal",
                extra={
                    "refund_outbox_event_id": event_id,
                    "compensation_case_id": case_id,
                    "refund_id": result.refund_id,
                    "reason_code": result.reason_code,
                },
            )
            return RefundWorkerCycle(True, True, result.reason_code, result.refund_id)

    async def _release(
        self,
        outbox: RefundOutboxEventRepository,
        *,
        event_id: str,
        expected_generation: int,
        reason_code: str,
    ) -> None:
        current = await outbox.get(event_id)
        if current is None or current.processed_at is not None:
            return
        try:
            await outbox.release_for_retry(
                current,
                expected_lease_generation=expected_generation,
                available_at=self._read_clock() + self._retry_delay(current.attempt_count),
                error_code=reason_code,
            )
        except ValueError:
            # A newer lease owner is authoritative; never mutate its work.
            return

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            result = await self.run_once()
            await record_worker_heartbeat(
                self._database,
                worker_type="refund",
                instance_id=self._instance_id,
                successful_work=result.processed,
                now=self._read_clock(),
            )
            if result.claimed:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._settings.refund_worker_poll_seconds,
                )
            except TimeoutError:
                continue

    def _retry_delay(self, attempt_count: int) -> timedelta:
        exponent = max(0, min(attempt_count - 1, 20))
        seconds = min(
            self._settings.refund_outbox_retry_base_seconds * (2**exponent),
            self._settings.refund_outbox_retry_max_seconds,
        )
        return timedelta(seconds=seconds)

    def _read_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Refund worker clock must be timezone-aware")
        return value.astimezone(UTC)


@asynccontextmanager
async def build_refund_worker_runtime():
    settings = get_settings()
    payment_provider = build_razorpay_payment_provider(settings)
    provider = payment_provider if isinstance(payment_provider, RefundProvider) else None
    database = create_database(settings)
    try:
        yield RefundOutboxWorker(database, settings, provider)
    finally:
        await database.dispose()


async def run_worker_cli(*, stop_event: asyncio.Event | None = None) -> None:
    owns_stop_event = stop_event is None
    effective_stop_event = stop_event or asyncio.Event()
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
        async with build_refund_worker_runtime() as worker:
            await worker.run_forever(effective_stop_event)
    finally:
        for signal_number in installed_signals:
            loop.remove_signal_handler(signal_number)


def main() -> None:
    try:
        asyncio.run(run_worker_cli())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
