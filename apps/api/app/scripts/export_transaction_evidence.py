"""Export one transaction's sanitized, reason-coded evidence as JSON."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import anyio
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import create_database
from app.models import (
    CompensationCase,
    CompensationEvent,
    Entitlement,
    FulfillmentEvent,
    FulfillmentExecution,
    PaymentRefund,
    PaymentTransaction,
    PaymentTransactionEvent,
)


def timestamp(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


async def export(transaction_id: str, output: Path) -> None:
    database = create_database(get_settings())
    try:
        async with database.session() as session:
            transaction = await session.get(PaymentTransaction, transaction_id)
            if transaction is None:
                raise SystemExit("Transaction was not found")
            payment_events = list(
                (
                    await session.scalars(
                        select(PaymentTransactionEvent)
                        .where(PaymentTransactionEvent.transaction_id == transaction_id)
                        .order_by(PaymentTransactionEvent.occurred_at)
                    )
                ).all()
            )
            entitlement = await session.scalar(
                select(Entitlement).where(Entitlement.transaction_id == transaction_id)
            )
            execution = await session.scalar(
                select(FulfillmentExecution).where(
                    FulfillmentExecution.transaction_id == transaction_id
                )
            )
            fulfillment_events = list(
                (
                    await session.scalars(
                        select(FulfillmentEvent)
                        .where(FulfillmentEvent.transaction_id == transaction_id)
                        .order_by(FulfillmentEvent.occurred_at)
                    )
                ).all()
            )
            case = await session.scalar(
                select(CompensationCase).where(CompensationCase.transaction_id == transaction_id)
            )
            refunds = list(
                (
                    await session.scalars(
                        select(PaymentRefund)
                        .where(PaymentRefund.transaction_id == transaction_id)
                        .order_by(PaymentRefund.created_at)
                    )
                ).all()
            )
            compensation_events = (
                []
                if case is None
                else list(
                    (
                        await session.scalars(
                            select(CompensationEvent)
                            .where(CompensationEvent.compensation_case_id == case.id)
                            .order_by(CompensationEvent.sequence)
                        )
                    ).all()
                )
            )
            stages = {
                "quote": bool(transaction.quote_id),
                "policy": bool(transaction.policy_id),
                "evaluation": bool(transaction.evaluation_id),
                "human_approval": bool(transaction.authorization_id),
                "payment_transaction": True,
                "provider_payment": any(
                    str(event.event_type) in {"payment_captured", "payment_reverified"}
                    for event in payment_events
                ),
                "entitlement": entitlement is not None,
                "fulfillment": execution is not None,
                "compensation_or_refund": case is not None or bool(refunds),
            }
            payload = {
                "export_version": "1",
                "generated_at": datetime.now(UTC).isoformat(),
                "transaction": {
                    "id": transaction.id,
                    "account_id": transaction.account_id,
                    "authorization_id": transaction.authorization_id,
                    "evaluation_id": transaction.evaluation_id,
                    "policy_id": transaction.policy_id,
                    "policy_hash": transaction.policy_hash,
                    "quote_id": transaction.quote_id,
                    "quote_hash": transaction.quote_hash,
                    "merchant_id": transaction.merchant_id,
                    "service_id": transaction.service_id,
                    "amount": transaction.amount,
                    "currency": transaction.currency,
                    "purchase_type": str(transaction.purchase_type),
                    "payment_state": str(transaction.transaction_state),
                    "provider_order_status": str(transaction.provider_order_status)
                    if transaction.provider_order_status
                    else None,
                    "payment_binding_hash": transaction.payment_binding_hash,
                },
                "audit_completeness": stages,
                "payment_events": [
                    {
                        "id": event.id,
                        "type": str(event.event_type),
                        "reason_code": event.reason_code,
                        "prior_state": str(event.prior_state) if event.prior_state else None,
                        "resulting_state": str(event.resulting_state),
                        "occurred_at": timestamp(event.occurred_at),
                    }
                    for event in payment_events
                ],
                "entitlement": None
                if entitlement is None
                else {
                    "id": entitlement.id,
                    "entitlement_hash": entitlement.entitlement_hash,
                    "expires_at": timestamp(entitlement.expires_at),
                },
                "fulfillment": None
                if execution is None
                else {
                    "id": execution.id,
                    "state": str(execution.execution_state),
                    "result_hash": execution.result_hash,
                    "failure_code": execution.failure_code,
                },
                "fulfillment_events": [
                    {
                        "id": event.id,
                        "type": str(event.event_type),
                        "reason_code": event.reason_code,
                        "occurred_at": timestamp(event.occurred_at),
                    }
                    for event in fulfillment_events
                ],
                "compensation": None
                if case is None
                else {
                    "id": case.id,
                    "state": str(case.decision_state),
                    "failure_code": case.failure_code,
                    "failure_evidence_hash": case.failure_evidence_hash,
                },
                "refunds": [
                    {
                        "id": refund.id,
                        "state": str(refund.refund_state),
                        "amount": refund.amount,
                        "currency": refund.currency,
                        "provider_status": refund.provider_status,
                        "provider_refund_id": refund.provider_refund_id,
                    }
                    for refund in refunds
                ],
                "compensation_events": [
                    {
                        "id": event.id,
                        "type": str(event.event_type),
                        "reason_code": event.reason_code,
                        "occurred_at": timestamp(event.occurred_at),
                    }
                    for event in compensation_events
                ],
            }
            await anyio.Path(output.parent).mkdir(parents=True, exist_ok=True)
            await anyio.Path(output).write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            print(f"Sanitized evidence exported to {output}")
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("transaction_id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(export(args.transaction_id, args.output.resolve()))


if __name__ == "__main__":
    main()
