"""PostgreSQL outbox worker for paid-entitlement issuance."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings, get_settings
from app.db.session import Database, create_database
from app.domain.exceptions import EntitlementError
from app.repositories import CommerceOutboxEventRepository
from app.services.capabilities import CapabilityTokenService
from app.services.entitlements import EntitlementApplicationService
from app.services.fulfillments import FulfillmentExpiryFinalizer
from app.services.payments import PaymentApplicationService, build_razorpay_payment_provider

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class EntitlementWorkerCycle:
    claimed: bool
    processed: bool
    reason_code: str | None
    finalized_expiration: bool = False


class EntitlementOutboxWorker:
    """Claim one due row at a time with SKIP LOCKED and lease fencing."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        payment_provider: object | None,
        *,
        clock: Clock = utc_now,
        logger: logging.Logger | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._payment_provider = payment_provider
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    async def run_once(self) -> EntitlementWorkerCycle:
        now = self._read_clock()
        async with self._database.session() as session:
            finalized = await FulfillmentExpiryFinalizer(
                session,
                clock=self._clock,
            ).finalize_one()
            issuance_enabled = (
                self._settings.fulfillment_enabled
                and self._settings.payments_enabled
                and self._settings.entitlement_token_secret is not None
                and self._payment_provider is not None
            )
            if not issuance_enabled:
                return EntitlementWorkerCycle(
                    False,
                    False,
                    "ENTITLEMENT_ISSUANCE_DISABLED",
                    finalized_expiration=finalized is not None,
                )
            outbox = CommerceOutboxEventRepository(session)
            event = await outbox.claim_next(
                now=now,
                lease_expires_at=now
                + timedelta(seconds=self._settings.entitlement_outbox_lease_seconds),
            )
            if event is None:
                return EntitlementWorkerCycle(
                    False,
                    False,
                    None,
                    finalized_expiration=finalized is not None,
                )
            event_id = event.id
            transaction_id = event.aggregate_id
            generation = event.lease_generation
            payment_service = PaymentApplicationService(
                session,
                self._payment_provider,  # type: ignore[arg-type]
                self._settings,
                clock=self._clock,
            )
            assert self._settings.entitlement_token_secret is not None
            capability_service = CapabilityTokenService(
                self._settings.entitlement_token_secret.get_secret_value(),
                ttl=timedelta(seconds=self._settings.entitlement_token_ttl_seconds),
                clock=self._clock,
            )
            service = EntitlementApplicationService(
                session,
                payment_service,
                capability_service,
                entitlement_ttl=timedelta(seconds=self._settings.entitlement_ttl_seconds),
                clock=self._clock,
            )
            try:
                entitlement = await service.issue_from_claimed_outbox(
                    event,
                    expected_lease_generation=generation,
                )
            except EntitlementError as error:
                await session.rollback()
                current = await outbox.get(event_id)
                if current is not None and current.processed_at is None:
                    delay = self._retry_delay(current.attempt_count)
                    try:
                        await outbox.release_for_retry(
                            current,
                            expected_lease_generation=generation,
                            available_at=self._read_clock() + delay,
                            error_code=error.reason_code,
                        )
                    except ValueError:
                        await session.rollback()
                self._logger.warning(
                    "entitlement_issuance_deferred",
                    extra={
                        "outbox_event_id": event_id,
                        "transaction_id": transaction_id,
                        "reason_code": error.reason_code,
                    },
                )
                return EntitlementWorkerCycle(
                    True,
                    False,
                    error.reason_code,
                    finalized_expiration=finalized is not None,
                )
            self._logger.info(
                "entitlement_issued",
                extra={
                    "outbox_event_id": event_id,
                    "transaction_id": transaction_id,
                    "entitlement_id": entitlement.id,
                },
            )
            return EntitlementWorkerCycle(
                True,
                True,
                None,
                finalized_expiration=finalized is not None,
            )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            result = await self.run_once()
            if result.claimed or result.finalized_expiration:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._settings.entitlement_worker_poll_seconds,
                )
            except TimeoutError:
                continue

    def _retry_delay(self, attempt_count: int) -> timedelta:
        exponent = max(0, min(attempt_count - 1, 20))
        seconds = min(
            self._settings.entitlement_outbox_retry_base_seconds * (2**exponent),
            self._settings.entitlement_outbox_retry_max_seconds,
        )
        return timedelta(seconds=seconds)

    def _read_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Entitlement worker clock must be timezone-aware")
        return value.astimezone(UTC)


@asynccontextmanager
async def build_entitlement_worker_runtime():
    settings = get_settings()
    provider = build_razorpay_payment_provider(settings)
    database = create_database(settings)
    try:
        yield EntitlementOutboxWorker(database, settings, provider)
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
        async with build_entitlement_worker_runtime() as worker:
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
