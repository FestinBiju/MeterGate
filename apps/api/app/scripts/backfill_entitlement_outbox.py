"""Idempotently enqueue entitlement work for pre-Milestone-7 paid transactions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.db.session import Database, create_database
from app.domain.enums import (
    CommerceAggregateType,
    CommerceOutboxEventType,
    PaymentTransactionState,
)
from app.domain.ids import new_commerce_outbox_event_id
from app.models import CommerceOutboxEvent, Entitlement, PaymentTransaction


async def backfill_entitlement_outbox(database: Database) -> int:
    """Insert at most one durable work record for every eligible historical payment."""
    now = datetime.now(UTC)
    async with database.session() as session:
        rows = await session.execute(
            select(PaymentTransaction.id, PaymentTransaction.payment_binding_hash)
            .outerjoin(Entitlement, Entitlement.transaction_id == PaymentTransaction.id)
            .outerjoin(
                CommerceOutboxEvent,
                CommerceOutboxEvent.deduplication_key == ("entitlement:" + PaymentTransaction.id),
            )
            .where(
                PaymentTransaction.transaction_state == PaymentTransactionState.PAID,
                Entitlement.id.is_(None),
                CommerceOutboxEvent.id.is_(None),
            )
            .order_by(PaymentTransaction.created_at, PaymentTransaction.id)
            .with_for_update(of=PaymentTransaction, skip_locked=True)
        )
        inserted = 0
        for transaction_id, payment_binding_hash in rows:
            statement = (
                insert(CommerceOutboxEvent)
                .values(
                    id=new_commerce_outbox_event_id(),
                    event_type=CommerceOutboxEventType.ENTITLEMENT_ISSUANCE_REQUESTED,
                    aggregate_type=CommerceAggregateType.PAYMENT_TRANSACTION,
                    aggregate_id=transaction_id,
                    deduplication_key=f"entitlement:{transaction_id}",
                    payload_version="1",
                    payload={
                        "transaction_id": transaction_id,
                        "payment_binding_hash": payment_binding_hash,
                    },
                    created_at=now,
                    available_at=now,
                    processing_started_at=None,
                    processed_at=None,
                    attempt_count=0,
                    lease_generation=0,
                    lease_expires_at=None,
                    last_error_code=None,
                )
                .on_conflict_do_nothing(index_elements=["deduplication_key"])
            )
            result = await session.execute(statement)
            inserted += result.rowcount or 0
        await session.commit()
    return inserted


async def _run() -> None:
    database = create_database(get_settings())
    try:
        inserted = await backfill_entitlement_outbox(database)
    finally:
        await database.dispose()
    print(f"Entitlement outbox backfill complete: inserted={inserted}.")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
