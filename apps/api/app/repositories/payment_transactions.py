"""Atomic persistence for the payment transaction aggregate and audit stream."""

from collections.abc import Sequence

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import PaymentTransactionState
from app.models import (
    PaymentAttempt,
    PaymentTransaction,
    PaymentTransactionEvent,
    RazorpayWebhookEvent,
)


class PaymentTransactionRepository:
    """Lock payment aggregates and commit every revision with append-only evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, transaction_id: str) -> PaymentTransaction | None:
        return await self._session.get(PaymentTransaction, transaction_id)

    async def get_for_update(self, transaction_id: str) -> PaymentTransaction | None:
        result = await self._session.scalars(
            select(PaymentTransaction)
            .where(PaymentTransaction.id == transaction_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.one_or_none()

    async def get_by_authorization_id(
        self,
        authorization_id: str,
    ) -> PaymentTransaction | None:
        result = await self._session.scalars(
            select(PaymentTransaction).where(
                PaymentTransaction.authorization_id == authorization_id
            )
        )
        return result.one_or_none()

    async def get_by_authorization_id_for_update(
        self,
        authorization_id: str,
    ) -> PaymentTransaction | None:
        result = await self._session.scalars(
            select(PaymentTransaction)
            .where(PaymentTransaction.authorization_id == authorization_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.one_or_none()

    async def get_by_provider_order_id(
        self,
        provider_order_id: str,
    ) -> PaymentTransaction | None:
        result = await self._session.scalars(
            select(PaymentTransaction).where(
                PaymentTransaction.provider_order_id == provider_order_id
            )
        )
        return result.one_or_none()

    async def get_by_provider_order_id_for_update(
        self,
        provider_order_id: str,
    ) -> PaymentTransaction | None:
        result = await self._session.scalars(
            select(PaymentTransaction)
            .where(PaymentTransaction.provider_order_id == provider_order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.one_or_none()

    async def get_by_provider_receipt(
        self,
        provider_receipt: str,
    ) -> PaymentTransaction | None:
        result = await self._session.scalars(
            select(PaymentTransaction).where(
                PaymentTransaction.provider_receipt == provider_receipt
            )
        )
        return result.one_or_none()

    async def create_with_event(
        self,
        transaction: PaymentTransaction,
        *,
        event: PaymentTransactionEvent,
    ) -> PaymentTransaction:
        """Atomically claim an authorization and create aggregate revision one."""
        self._require_available_session(transaction, "Payment transaction")
        self._require_available_session(event, "Payment transaction event")
        if transaction.revision != 1:
            raise ValueError("A new payment transaction must start at revision 1")
        if event.transaction_id != transaction.id or event.transaction_revision != 1:
            raise ValueError("Initial payment event must describe transaction revision 1")
        if event.prior_state is not None:
            raise ValueError("Initial payment event cannot have a prior state")
        if PaymentTransactionState(event.resulting_state) is not PaymentTransactionState(
            transaction.transaction_state
        ):
            raise ValueError("Initial payment event state does not match the transaction")

        self._session.add(transaction)
        try:
            await self._session.flush()
            self._session.add(event)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(transaction)
        await self._session.refresh(event)
        return transaction

    async def update_with_event(
        self,
        transaction: PaymentTransaction,
        *,
        event: PaymentTransactionEvent,
        attempt: PaymentAttempt | None = None,
        attempts: Sequence[PaymentAttempt] = (),
        webhook_event: RazorpayWebhookEvent | None = None,
    ) -> PaymentTransaction:
        """Commit one aggregate revision and all associated evidence atomically.

        ``transaction`` must have been locked on this session. The caller applies
        desired lifecycle changes but leaves ``revision`` unchanged; this method
        owns the single revision increment.
        """
        self._require_bound_session(transaction, "Payment transaction")
        self._require_available_session(event, "Payment transaction event")

        state = sqlalchemy_inspect(transaction)
        state_history = state.attrs.transaction_state.history
        prior_state = (
            state_history.deleted[0]
            if state_history.has_changes() and state_history.deleted
            else transaction.transaction_state
        )
        next_revision = transaction.revision + 1
        if event.transaction_id != transaction.id:
            raise ValueError("Payment event references a different transaction")
        if event.transaction_revision != next_revision:
            raise ValueError("Payment event revision does not match the next aggregate revision")
        if PaymentTransactionState(event.prior_state) is not PaymentTransactionState(prior_state):
            raise ValueError("Payment event prior state does not match the aggregate")
        if PaymentTransactionState(event.resulting_state) is not PaymentTransactionState(
            transaction.transaction_state
        ):
            raise ValueError("Payment event resulting state does not match the aggregate")

        prerequisite_records: list[object] = [transaction]
        attempt_records = list(attempts)
        if attempt is not None:
            attempt_records.append(attempt)
        attempt_ids = [payment_attempt.id for payment_attempt in attempt_records]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("Payment attempt evidence cannot be supplied more than once")
        for payment_attempt in attempt_records:
            self._require_available_session(payment_attempt, "Payment attempt")
            if payment_attempt.transaction_id != transaction.id:
                raise ValueError("Payment attempt references a different transaction")
            if payment_attempt.provider_order_id != transaction.provider_order_id:
                raise ValueError("Payment attempt references a different provider order")
            prerequisite_records.append(payment_attempt)
        if (
            event.payment_attempt_id is not None
            and attempt_records
            and event.payment_attempt_id not in attempt_ids
        ):
            raise ValueError("Payment event references a different payment attempt")
        if webhook_event is not None:
            self._require_available_session(webhook_event, "Razorpay webhook event")
            if webhook_event.transaction_id != transaction.id:
                raise ValueError("Webhook evidence references a different transaction")
            if (
                event.source_webhook_event_id is not None
                and event.source_webhook_event_id != webhook_event.id
            ):
                raise ValueError("Payment event references different webhook evidence")
            prerequisite_records.append(webhook_event)

        transaction.revision = next_revision
        self._session.add_all(prerequisite_records)
        try:
            await self._session.flush()
            self._session.add(event)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        for record in (*prerequisite_records, event):
            await self._session.refresh(record)
        return transaction

    def _require_available_session(self, record: object, label: str) -> None:
        bound_session = sqlalchemy_inspect(record).session
        if bound_session not in {None, self._session.sync_session}:
            raise ValueError(f"{label} belongs to a different database session")

    def _require_bound_session(self, record: object, label: str) -> None:
        if sqlalchemy_inspect(record).session is not self._session.sync_session:
            raise ValueError(f"{label} must be loaded on the repository database session")
