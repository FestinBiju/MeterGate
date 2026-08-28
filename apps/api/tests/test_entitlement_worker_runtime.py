"""Runtime degradation behavior for the entitlement/finalization worker."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.core.config import Settings
from app.workers import entitlements as worker_module


class FakeDatabase:
    def __init__(self) -> None:
        self.session_value = object()

    @asynccontextmanager
    async def session(self):
        yield self.session_value


@pytest.mark.asyncio
async def test_disabled_issuance_still_runs_expiry_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalized = object()
    calls: list[object] = []

    class RecordingFinalizer:
        def __init__(self, session: object, **_: object) -> None:
            calls.append(session)

        async def finalize_one(self) -> object:
            return finalized

    class OutboxMustNotBeClaimed:
        def __init__(self, _session: object) -> None:
            raise AssertionError("disabled issuance must not claim outbox work")

    monkeypatch.setattr(worker_module, "FulfillmentExpiryFinalizer", RecordingFinalizer)
    monkeypatch.setattr(
        worker_module,
        "CommerceOutboxEventRepository",
        OutboxMustNotBeClaimed,
    )
    database = FakeDatabase()
    settings = Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        redis_url="redis://localhost:6379/15",
        payments_enabled=False,
        fulfillment_enabled=False,
        cors_allowed_origins=["http://localhost:3000"],
        webauthn_expected_origins=["http://localhost:3000"],
        _env_file=None,
    )
    worker = worker_module.EntitlementOutboxWorker(
        database,  # type: ignore[arg-type]
        settings,
        None,
    )

    cycle = await worker.run_once()

    assert calls == [database.session_value]
    assert cycle.claimed is False
    assert cycle.processed is False
    assert cycle.reason_code == "ENTITLEMENT_ISSUANCE_DISABLED"
    assert cycle.finalized_expiration is True
