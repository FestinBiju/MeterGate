"""Lightweight durable worker heartbeat writes with safe queue counts."""

from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db.session import Database
from app.models import CommerceOutboxEvent, RefundOutboxEvent, WorkerHeartbeat

WORKER_TYPES = ("razorpay_webhook", "entitlement", "refund")


async def record_worker_heartbeat(
    database: Database,
    *,
    worker_type: str,
    instance_id: str,
    successful_work: bool,
    now: datetime,
) -> None:
    """Upsert one process-scoped heartbeat without exposing host metadata."""
    if worker_type not in WORKER_TYPES:
        raise ValueError("Worker type is not recognized")
    current = now.astimezone(UTC)
    async with database.session() as session:
        heartbeat = await session.scalar(
            select(WorkerHeartbeat)
            .where(
                WorkerHeartbeat.worker_type == worker_type,
                WorkerHeartbeat.instance_id == instance_id,
            )
            .with_for_update()
        )
        backlog = 0
        if worker_type == "entitlement":
            backlog = (
                await session.scalar(
                    select(func.count())
                    .select_from(CommerceOutboxEvent)
                    .where(CommerceOutboxEvent.processed_at.is_(None))
                )
                or 0
            )
        elif worker_type == "refund":
            backlog = (
                await session.scalar(
                    select(func.count())
                    .select_from(RefundOutboxEvent)
                    .where(RefundOutboxEvent.processed_at.is_(None))
                )
                or 0
            )
        if heartbeat is None:
            heartbeat = WorkerHeartbeat(
                worker_type=worker_type,
                instance_id=instance_id,
                last_heartbeat=current,
                last_successful_work=current if successful_work else None,
                backlog_count=backlog,
            )
            session.add(heartbeat)
        else:
            heartbeat.last_heartbeat = current
            heartbeat.backlog_count = backlog
            if successful_work:
                heartbeat.last_successful_work = current
        await session.commit()
