"""Unit coverage for the durable refund outbox worker."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest

from app.core.config import Settings
from app.domain.enums import PaymentRefundState
from app.domain.exceptions import CompensationUnavailableError
from app.services.compensations import RefundExecutionResult
from app.workers import refunds as worker_module

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)


def worker_settings(
    *,
    refunds_enabled: bool = True,
    retry_base_seconds: int = 5,
    retry_max_seconds: int = 300,
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        redis_url="redis://localhost:6379/15",
        payments_enabled=True,
        refunds_enabled=refunds_enabled,
        razorpay_key_id="rzp_test_12345678",
        razorpay_key_secret="worker-test-key-secret",
        razorpay_webhook_secret="worker-test-webhook-secret",
        refund_outbox_retry_base_seconds=retry_base_seconds,
        refund_outbox_retry_max_seconds=retry_max_seconds,
        cors_allowed_origins=["http://localhost:3000"],
        webauthn_expected_origins=["http://localhost:3000"],
        _env_file=None,
    )


class FakeSession:
    def __init__(self) -> None:
        self.rollback_calls = 0

    async def rollback(self) -> None:
        self.rollback_calls += 1


class FakeDatabase:
    def __init__(self) -> None:
        self.session_value = FakeSession()
        self.session_entries = 0

    @asynccontextmanager
    async def session(self) -> AsyncIterator[FakeSession]:
        self.session_entries += 1
        yield self.session_value


class DatabaseMustNotBeOpened:
    @asynccontextmanager
    async def session(self) -> AsyncIterator[FakeSession]:
        raise AssertionError("refund worker must not open a database session")
        yield FakeSession()  # pragma: no cover


@dataclass(slots=True)
class FakeOutboxEvent:
    id: str = "rox_0" + "A" * 25
    compensation_case_id: str = "cmp_0" + "B" * 25
    lease_generation: int = 4
    attempt_count: int = 1
    processed_at: datetime | None = None


class RecordingOutbox:
    def __init__(
        self,
        events: tuple[FakeOutboxEvent, ...] = (),
        *,
        current_missing: bool = False,
        fail_release: bool = False,
        fail_mark_processed: bool = False,
    ) -> None:
        self._events = deque(events)
        self._current = {event.id: event for event in events}
        self.current_missing = current_missing
        self.fail_release = fail_release
        self.fail_mark_processed = fail_mark_processed
        self.claim_calls: list[tuple[datetime, datetime]] = []
        self.get_calls: list[str] = []
        self.release_calls: list[tuple[str, int, datetime, str]] = []
        self.mark_processed_calls: list[tuple[str, int, datetime]] = []

    async def claim_next(
        self,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> FakeOutboxEvent | None:
        self.claim_calls.append((now, lease_expires_at))
        return self._events.popleft() if self._events else None

    async def get(self, event_id: str) -> FakeOutboxEvent | None:
        self.get_calls.append(event_id)
        if self.current_missing:
            return None
        return self._current.get(event_id)

    async def release_for_retry(
        self,
        event: FakeOutboxEvent,
        *,
        expected_lease_generation: int,
        available_at: datetime,
        error_code: str,
    ) -> FakeOutboxEvent:
        self.release_calls.append((event.id, expected_lease_generation, available_at, error_code))
        if self.fail_release:
            raise ValueError("Refund outbox lease is stale or already processed")
        return event

    async def mark_processed(
        self,
        event: FakeOutboxEvent,
        *,
        expected_lease_generation: int,
        processed_at: datetime,
    ) -> FakeOutboxEvent:
        self.mark_processed_calls.append((event.id, expected_lease_generation, processed_at))
        if self.fail_mark_processed:
            raise ValueError("Refund outbox lease is stale or already processed")
        event.processed_at = processed_at
        return event


class ScriptedRefundService:
    def __init__(self, outcomes: tuple[RefundExecutionResult | Exception, ...]) -> None:
        self._outcomes = deque(outcomes)
        self.case_ids: list[str] = []

    async def execute_case(self, case_id: str) -> RefundExecutionResult:
        self.case_ids.append(case_id)
        outcome = self._outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def install_worker_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database: FakeDatabase,
    outbox: RecordingOutbox,
    service: ScriptedRefundService | None,
    provider: object,
) -> None:
    monkeypatch.setattr(
        worker_module,
        "RefundOutboxEventRepository",
        lambda session: outbox if session is database.session_value else None,
    )

    def service_factory(
        session: object,
        actual_provider: object,
        settings: Settings,
        *,
        clock: object,
    ) -> ScriptedRefundService:
        del settings, clock
        assert service is not None
        assert session is database.session_value
        assert actual_provider is provider
        return service

    monkeypatch.setattr(worker_module, "RefundApplicationService", service_factory)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "provider"),
    [
        (worker_settings(refunds_enabled=False), object()),
        (worker_settings(), None),
    ],
)
async def test_disabled_execution_or_missing_provider_does_not_open_database(
    settings: Settings,
    provider: object | None,
) -> None:
    worker = worker_module.RefundOutboxWorker(
        DatabaseMustNotBeOpened(),  # type: ignore[arg-type]
        settings,
        provider,  # type: ignore[arg-type]
    )

    result = await worker.run_once()

    assert result == worker_module.RefundWorkerCycle(
        claimed=False,
        processed=False,
        reason_code="REFUND_EXECUTION_DISABLED",
    )


@pytest.mark.asyncio
async def test_empty_outbox_returns_without_constructing_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    outbox = RecordingOutbox()
    provider = object()
    install_worker_fakes(
        monkeypatch,
        database=database,
        outbox=outbox,
        service=None,
        provider=provider,
    )
    worker = worker_module.RefundOutboxWorker(
        database,  # type: ignore[arg-type]
        worker_settings(),
        provider,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result == worker_module.RefundWorkerCycle(False, False, None)
    assert outbox.claim_calls == [(NOW, NOW + timedelta(seconds=30))]
    assert database.session_entries == 1


@pytest.mark.asyncio
async def test_terminal_refund_marks_claim_processed_with_lease_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    event = FakeOutboxEvent(lease_generation=7, attempt_count=2)
    outbox = RecordingOutbox((event,))
    provider = object()
    service = ScriptedRefundService(
        (
            RefundExecutionResult(
                refund_id="rfd_0" + "C" * 25,
                state=PaymentRefundState.REFUNDED,
                terminal=True,
                reason_code="REFUND_COMPLETED",
            ),
        )
    )
    install_worker_fakes(
        monkeypatch,
        database=database,
        outbox=outbox,
        service=service,
        provider=provider,
    )
    worker = worker_module.RefundOutboxWorker(
        database,  # type: ignore[arg-type]
        worker_settings(),
        provider,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result == worker_module.RefundWorkerCycle(
        True,
        True,
        "REFUND_COMPLETED",
        "rfd_0" + "C" * 25,
    )
    assert service.case_ids == [event.compensation_case_id]
    assert outbox.mark_processed_calls == [(event.id, 7, NOW)]
    assert outbox.release_calls == []
    assert database.session_value.rollback_calls == 0


@pytest.mark.asyncio
async def test_nonterminal_refund_releases_with_capped_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    event = FakeOutboxEvent(lease_generation=9, attempt_count=3)
    outbox = RecordingOutbox((event,))
    provider = object()
    refund_id = "rfd_0" + "D" * 25
    service = ScriptedRefundService(
        (
            RefundExecutionResult(
                refund_id=refund_id,
                state=PaymentRefundState.REFUND_PROCESSING,
                terminal=False,
                reason_code="REFUND_PROCESSING",
            ),
        )
    )
    install_worker_fakes(
        monkeypatch,
        database=database,
        outbox=outbox,
        service=service,
        provider=provider,
    )
    worker = worker_module.RefundOutboxWorker(
        database,  # type: ignore[arg-type]
        worker_settings(retry_base_seconds=3, retry_max_seconds=10),
        provider,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result == worker_module.RefundWorkerCycle(
        True,
        False,
        "REFUND_PROCESSING",
        refund_id,
    )
    assert outbox.release_calls == [(event.id, 9, NOW + timedelta(seconds=10), "REFUND_PROCESSING")]
    assert outbox.mark_processed_calls == []


@pytest.mark.asyncio
async def test_compensation_error_rolls_back_logs_code_and_releases_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    event = FakeOutboxEvent(lease_generation=5, attempt_count=1)
    outbox = RecordingOutbox((event,))
    provider = object()
    service = ScriptedRefundService(
        (
            CompensationUnavailableError(
                "secret provider detail must not be logged",
                "REFUND_PROVIDER_UNAVAILABLE",
            ),
        )
    )
    install_worker_fakes(
        monkeypatch,
        database=database,
        outbox=outbox,
        service=service,
        provider=provider,
    )
    log_stream = StringIO()
    logger = logging.Logger("refund-worker-test", level=logging.WARNING)
    logger.addHandler(logging.StreamHandler(log_stream))
    worker = worker_module.RefundOutboxWorker(
        database,  # type: ignore[arg-type]
        worker_settings(retry_base_seconds=7),
        provider,  # type: ignore[arg-type]
        clock=lambda: NOW,
        logger=logger,
    )

    result = await worker.run_once()

    assert result == worker_module.RefundWorkerCycle(
        True,
        False,
        "REFUND_PROVIDER_UNAVAILABLE",
    )
    assert database.session_value.rollback_calls == 1
    assert outbox.release_calls == [
        (event.id, 5, NOW + timedelta(seconds=7), "REFUND_PROVIDER_UNAVAILABLE")
    ]
    logged = log_stream.getvalue()
    assert "refund_execution_deferred" in logged
    assert "secret provider detail" not in logged


@pytest.mark.asyncio
async def test_terminal_result_with_missing_outbox_rolls_back_and_reports_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    event = FakeOutboxEvent()
    outbox = RecordingOutbox((event,), current_missing=True)
    provider = object()
    refund_id = "rfd_0" + "E" * 25
    service = ScriptedRefundService(
        (
            RefundExecutionResult(
                refund_id=refund_id,
                state=PaymentRefundState.REFUNDED,
                terminal=True,
                reason_code="REFUND_COMPLETED",
            ),
        )
    )
    install_worker_fakes(
        monkeypatch,
        database=database,
        outbox=outbox,
        service=service,
        provider=provider,
    )
    worker = worker_module.RefundOutboxWorker(
        database,  # type: ignore[arg-type]
        worker_settings(),
        provider,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result == worker_module.RefundWorkerCycle(
        True,
        False,
        "REFUND_OUTBOX_MISSING",
        refund_id,
    )
    assert database.session_value.rollback_calls == 1
    assert outbox.mark_processed_calls == []


@pytest.mark.asyncio
async def test_stale_terminal_worker_cannot_mark_successor_lease_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    event = FakeOutboxEvent(lease_generation=11)
    outbox = RecordingOutbox((event,), fail_mark_processed=True)
    provider = object()
    refund_id = "rfd_0" + "F" * 25
    service = ScriptedRefundService(
        (
            RefundExecutionResult(
                refund_id=refund_id,
                state=PaymentRefundState.REFUNDED,
                terminal=True,
                reason_code="REFUND_COMPLETED",
            ),
        )
    )
    install_worker_fakes(
        monkeypatch,
        database=database,
        outbox=outbox,
        service=service,
        provider=provider,
    )
    worker = worker_module.RefundOutboxWorker(
        database,  # type: ignore[arg-type]
        worker_settings(),
        provider,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result == worker_module.RefundWorkerCycle(
        True,
        False,
        "REFUND_WORK_LEASE_LOST",
        refund_id,
    )
    assert database.session_value.rollback_calls == 1
    assert outbox.mark_processed_calls == [(event.id, 11, NOW)]


@pytest.mark.asyncio
async def test_stale_nonterminal_worker_does_not_overwrite_successor_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    event = FakeOutboxEvent(lease_generation=13, attempt_count=2)
    outbox = RecordingOutbox((event,), fail_release=True)
    provider = object()
    refund_id = "rfd_0" + "G" * 25
    service = ScriptedRefundService(
        (
            RefundExecutionResult(
                refund_id=refund_id,
                state=PaymentRefundState.REFUND_PROCESSING,
                terminal=False,
                reason_code="REFUND_PROCESSING",
            ),
        )
    )
    install_worker_fakes(
        monkeypatch,
        database=database,
        outbox=outbox,
        service=service,
        provider=provider,
    )
    worker = worker_module.RefundOutboxWorker(
        database,  # type: ignore[arg-type]
        worker_settings(),
        provider,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = await worker.run_once()

    assert result == worker_module.RefundWorkerCycle(
        True,
        False,
        "REFUND_PROCESSING",
        refund_id,
    )
    assert outbox.release_calls == [
        (event.id, 13, NOW + timedelta(seconds=10), "REFUND_PROCESSING")
    ]
    assert outbox.mark_processed_calls == []


@pytest.mark.asyncio
async def test_naive_worker_clock_is_rejected_before_database_access() -> None:
    worker = worker_module.RefundOutboxWorker(
        DatabaseMustNotBeOpened(),  # type: ignore[arg-type]
        worker_settings(),
        object(),  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 8, 26, 12),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        await worker.run_once()


@pytest.mark.asyncio
async def test_duplicate_poll_is_noop_after_terminal_work_is_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    event = FakeOutboxEvent()
    outbox = RecordingOutbox((event,))
    provider = object()
    refund_id = "rfd_0" + "H" * 25
    service = ScriptedRefundService(
        (
            RefundExecutionResult(
                refund_id=refund_id,
                state=PaymentRefundState.REFUNDED,
                terminal=True,
                reason_code="REFUND_COMPLETED",
            ),
        )
    )
    install_worker_fakes(
        monkeypatch,
        database=database,
        outbox=outbox,
        service=service,
        provider=provider,
    )
    worker = worker_module.RefundOutboxWorker(
        database,  # type: ignore[arg-type]
        worker_settings(),
        provider,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    first = await worker.run_once()
    duplicate = await worker.run_once()

    assert first == worker_module.RefundWorkerCycle(True, True, "REFUND_COMPLETED", refund_id)
    assert duplicate == worker_module.RefundWorkerCycle(False, False, None)
    assert service.case_ids == [event.compensation_case_id]
    assert len(outbox.mark_processed_calls) == 1
    assert len(outbox.claim_calls) == 2


def test_retry_delay_caps_extreme_attempt_counts() -> None:
    worker = worker_module.RefundOutboxWorker(
        DatabaseMustNotBeOpened(),  # type: ignore[arg-type]
        worker_settings(retry_base_seconds=3, retry_max_seconds=20),
        object(),  # type: ignore[arg-type]
    )

    assert worker._retry_delay(1) == timedelta(seconds=3)  # noqa: SLF001
    assert worker._retry_delay(2) == timedelta(seconds=6)  # noqa: SLF001
    assert worker._retry_delay(999) == timedelta(seconds=20)  # noqa: SLF001
