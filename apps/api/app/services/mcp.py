"""Scoped MCP buyer sessions and thin orchestration over existing commerce services."""

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.enums import AccountStatus, FulfillmentExecutionState, PolicyDecision
from app.domain.exceptions import (
    McpSessionConflictError,
    McpSessionForbiddenError,
    McpSessionNotFoundError,
    McpSessionUnauthorizedError,
)
from app.domain.hashing import sha256_bytes
from app.models import (
    Account,
    ApprovalIdentity,
    Entitlement,
    FulfillmentExecution,
    McpAgentSession,
    McpToolAuditEvent,
    PaymentTransaction,
    PurchaseAuthorization,
)
from app.providers import PaymentProvider
from app.providers.fulfillment import FulfillmentProvider, UnavailableFulfillmentProvider
from app.repositories import (
    BuyerPolicyRepository,
    PolicyEvaluationRepository,
    QuoteRepository,
    ServiceRepository,
)
from app.schemas.entitlements import (
    CapabilityResponse,
    EntitlementLookupResponse,
    EntitlementResponse,
    EntitlementTimelineEvent,
    FulfillmentPendingResponse,
    FulfillmentResultResponse,
    ResourceParty,
)
from app.schemas.mcp import (
    ALL_MCP_SCOPES,
    McpAgentSessionCreate,
    McpAgentSessionCreated,
    McpAgentSessionRenew,
    McpAgentSessionResponse,
    McpEvaluationResult,
    McpPurchaseStatusResponse,
    McpScope,
)
from app.schemas.policies import BuyerPolicyCreate
from app.schemas.policy_evaluations import PolicyEvaluationCreate
from app.schemas.quotes import QuoteCreate
from app.services.capabilities import CapabilityTokenService
from app.services.catalog import CatalogApplicationService
from app.services.entitlements import EntitlementApplicationService, EntitlementView
from app.services.fulfillments import FulfillmentApplicationService
from app.services.payments import PaymentApplicationService
from app.services.policies import BuyerPolicyApplicationService
from app.services.policy_evaluations import PolicyEvaluationApplicationService
from app.services.quotes import QuoteApplicationService
from app.services.resources import payment_required_projection

_TOKEN_PATTERN = re.compile(r"^mcp_[A-Za-z0-9_-]{43}$")
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_SAFE_RESOURCE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_AUDIT_RESOURCE_KEYS = frozenset(
    {
        "authorization_id",
        "entitlement_id",
        "evaluation_id",
        "execution_id",
        "merchant_slug",
        "policy_id",
        "quote_id",
        "service_id",
        "service_slug",
        "transaction_id",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedMcpSession:
    session: McpAgentSession
    account: Account
    approval_subject_ref: str

    @property
    def account_id(self) -> str:
        return self.account.id


class McpAgentSessionService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def create(
        self,
        payload: McpAgentSessionCreate,
        *,
        account_id: str,
    ) -> McpAgentSessionCreated:
        if payload.expires_in_seconds > self._settings.mcp_agent_session_max_ttl_seconds:
            raise McpSessionConflictError(
                "The requested agent session lifetime exceeds the configured maximum",
                "MCP_SESSION_TTL_EXCEEDED",
            )
        now = datetime.now(UTC)
        token = f"mcp_{secrets.token_urlsafe(32)}"
        if _TOKEN_PATTERN.fullmatch(token) is None:
            raise RuntimeError("Generated MCP bridge token has an invalid shape")
        record = McpAgentSession(
            account_id=account_id,
            token_hash=sha256_bytes(token.encode("utf-8")),
            scopes=list(payload.scopes),
            created_at=now,
            expires_at=now + timedelta(seconds=payload.expires_in_seconds),
        )
        self._session.add(record)
        await self._session.commit()
        return McpAgentSessionCreated(
            **self._response(record).model_dump(),
            token=token,
        )

    async def list(self, *, account_id: str) -> list[McpAgentSessionResponse]:
        records = (
            await self._session.scalars(
                select(McpAgentSession)
                .where(McpAgentSession.account_id == account_id)
                .order_by(McpAgentSession.created_at.desc())
            )
        ).all()
        return [self._response(record) for record in records]

    async def revoke(self, session_id: str, *, account_id: str) -> McpAgentSessionResponse:
        record = await self._session.scalar(
            select(McpAgentSession)
            .where(
                McpAgentSession.id == session_id,
                McpAgentSession.account_id == account_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise McpSessionNotFoundError(
                "The agent session was not found",
                "MCP_SESSION_NOT_FOUND",
            )
        if record.revoked_at is None:
            record.revoked_at = datetime.now(UTC)
            await self._session.commit()
        return self._response(record)

    async def resolve(
        self,
        token: str,
        *,
        required_scope: McpScope | None = None,
    ) -> ResolvedMcpSession:
        if _TOKEN_PATTERN.fullmatch(token) is None:
            raise McpSessionUnauthorizedError(
                "A valid MeterGate agent session is required",
                "MCP_SESSION_REQUIRED",
            )
        record = await self._session.scalar(
            select(McpAgentSession)
            .where(McpAgentSession.token_hash == sha256_bytes(token.encode("utf-8")))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise McpSessionUnauthorizedError(
                "A valid MeterGate agent session is required",
                "MCP_SESSION_REQUIRED",
            )
        now = datetime.now(UTC)
        if record.revoked_at is not None:
            raise McpSessionUnauthorizedError(
                "The MeterGate agent session was revoked",
                "MCP_SESSION_REVOKED",
            )
        if self._utc(record.expires_at) <= now:
            raise McpSessionUnauthorizedError(
                "Agent access expired. Renew this connection with your passkey at "
                f"{self._settings.mcp_frontend_base_url.rstrip('/')}/"
                f"#agent-session-{record.id}. Keep the existing MCP token and configuration.",
                "MCP_SESSION_EXPIRED",
            )
        account = await self._session.get(Account, record.account_id)
        if account is None or AccountStatus(account.status) is not AccountStatus.ACTIVE:
            raise McpSessionUnauthorizedError(
                "The buyer account is not active",
                "MCP_SESSION_REVOKED",
            )
        identity = await self._session.scalar(
            select(ApprovalIdentity).where(ApprovalIdentity.account_id == account.id)
        )
        if identity is None or identity.status != "active":
            raise McpSessionUnauthorizedError(
                "The buyer approval identity is not active",
                "MCP_SESSION_REVOKED",
            )
        scopes = self._validated_scopes(record.scopes)
        if required_scope is not None and required_scope not in scopes:
            raise McpSessionForbiddenError(
                "The agent session does not grant the required scope",
                "MCP_SCOPE_DENIED",
            )
        record.last_used_at = now
        await self._session.commit()
        return ResolvedMcpSession(
            session=record,
            account=account,
            approval_subject_ref=identity.subject_ref,
        )

    async def get_renewable(self, session_id: str, *, account_id: str) -> McpAgentSession:
        """Lock owner-bound metadata; a revoked credential can never be renewed."""
        record = await self._session.scalar(
            select(McpAgentSession)
            .where(McpAgentSession.id == session_id, McpAgentSession.account_id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None or record.account_id != account_id:
            raise McpSessionNotFoundError(
                "The agent session was not found", "MCP_SESSION_NOT_FOUND"
            )
        if record.revoked_at is not None:
            raise McpSessionConflictError(
                "Revoked connections cannot be renewed. Configure a new connection instead.",
                "MCP_SESSION_REVOKED",
            )
        self._validated_scopes(record.scopes)
        return record

    async def renew(
        self, record: McpAgentSession, payload: McpAgentSessionRenew
    ) -> McpAgentSessionResponse:
        """Commit the grant and consumed passkey proof together, preserving the token hash."""
        if payload.expires_in_seconds > self._settings.mcp_agent_session_max_ttl_seconds:
            raise McpSessionConflictError(
                "The requested agent session lifetime exceeds the configured maximum",
                "MCP_SESSION_TTL_EXCEEDED",
            )
        if record.revoked_at is not None:
            raise McpSessionConflictError(
                "Revoked connections cannot be renewed", "MCP_SESSION_REVOKED"
            )
        now = datetime.now(UTC)
        previous_expiry = self._utc(record.expires_at)
        record.expires_at = now + timedelta(seconds=payload.expires_in_seconds)
        self._session.add(
            McpToolAuditEvent(
                agent_session_id=record.id,
                account_id=record.account_id,
                tool_name="renew_agent_session",
                correlation_id=payload.human_presence_proof_id,
                resource_ids={
                    "human_presence_proof_id": payload.human_presence_proof_id,
                    "previous_expires_at": previous_expiry.isoformat(),
                    "expires_at": record.expires_at.isoformat(),
                },
                result_code="MCP_SESSION_RENEWED",
                occurred_at=now,
            )
        )
        await self._session.commit()
        return self._response(record)

    @staticmethod
    def _validated_scopes(value: object) -> frozenset[McpScope]:
        allowed = frozenset(ALL_MCP_SCOPES)
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(scope, str) or scope not in allowed for scope in value)
        ):
            raise McpSessionUnauthorizedError(
                "The agent session scope evidence is invalid",
                "MCP_SESSION_REVOKED",
            )
        return frozenset(value)  # type: ignore[arg-type]

    @classmethod
    def _response(cls, record: McpAgentSession) -> McpAgentSessionResponse:
        now = datetime.now(UTC)
        state = (
            "revoked"
            if record.revoked_at is not None
            else "expired"
            if cls._utc(record.expires_at) <= now
            else "active"
        )
        return McpAgentSessionResponse(
            id=record.id,
            account_id=record.account_id,
            scopes=[
                scope for scope in ALL_MCP_SCOPES if scope in cls._validated_scopes(record.scopes)
            ],
            created_at=cls._utc(record.created_at),
            expires_at=cls._utc(record.expires_at),
            last_used_at=cls._utc(record.last_used_at) if record.last_used_at else None,
            revoked_at=cls._utc(record.revoked_at) if record.revoked_at else None,
            state=state,
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class McpAuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        principal: ResolvedMcpSession,
        *,
        tool_name: str,
        correlation_id: str,
        resource_ids: dict[str, str | None],
        result_code: str,
    ) -> None:
        safe_ids = {
            key: value
            for key, value in resource_ids.items()
            if value is not None
            and key in _AUDIT_RESOURCE_KEYS
            and _SAFE_RESOURCE_ID.fullmatch(value)
        }
        safe_code = result_code if _SAFE_CODE.fullmatch(result_code) else "MCP_TOOL_FAILED"
        self._session.add(
            McpToolAuditEvent(
                agent_session_id=principal.session.id,
                account_id=principal.account_id,
                tool_name=tool_name[:64],
                correlation_id=correlation_id[:64],
                resource_ids=safe_ids,
                result_code=safe_code,
                occurred_at=datetime.now(UTC),
            )
        )
        await self._session.commit()


class McpCommerceService:
    """Thin MCP projection that delegates every commerce decision to existing services."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        payment_provider: PaymentProvider | None,
        fulfillment_provider: FulfillmentProvider | None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._payment = PaymentApplicationService(session, payment_provider, settings)
        self._catalog = CatalogApplicationService(ServiceRepository(session))
        self._quotes = QuoteApplicationService(
            ServiceRepository(session),
            QuoteRepository(session),
            ttl=timedelta(seconds=settings.quote_ttl_seconds),
        )
        self._policies = BuyerPolicyApplicationService(
            BuyerPolicyRepository(session),
            maximum_ttl=timedelta(seconds=settings.policy_max_ttl_seconds),
        )
        self._evaluations = PolicyEvaluationApplicationService(
            BuyerPolicyRepository(session),
            QuoteRepository(session),
            PolicyEvaluationRepository(session),
        )
        self._entitlements: EntitlementApplicationService | None = None
        self._fulfillments: FulfillmentApplicationService | None = None
        if settings.fulfillment_enabled and settings.entitlement_token_secret is not None:
            capability_service = CapabilityTokenService(
                settings.entitlement_token_secret.get_secret_value(),
                ttl=timedelta(seconds=settings.entitlement_token_ttl_seconds),
            )
            self._entitlements = EntitlementApplicationService(
                session,
                self._payment,
                capability_service,
                entitlement_ttl=timedelta(seconds=settings.entitlement_ttl_seconds),
            )
            self._fulfillments = FulfillmentApplicationService(
                session,
                capability_service,
                fulfillment_provider or UnavailableFulfillmentProvider(),
                payment_eligibility=self._payment,
                provider_base_url=settings.orbitintel_base_url,
                execution_lease=timedelta(seconds=settings.fulfillment_execution_lease_seconds),
                maximum_result_bytes=settings.fulfillment_max_result_bytes,
                default_maximum_attempts=settings.fulfillment_max_attempts,
            )

    async def list_services(self):
        return await self._catalog.list()

    async def get_service(self, service_id: str):
        return await self._catalog.get(service_id)

    async def inspect_payment_requirement(
        self, merchant_slug: str, service_slug: str, input_value: object
    ):
        return await payment_required_projection(
            self._session,
            merchant_slug=merchant_slug,
            service_slug=service_slug,
            input_value=input_value,
        )

    async def request_quote(self, payload: QuoteCreate):
        return await self._quotes.create(payload)

    async def create_policy(self, payload: BuyerPolicyCreate, *, account_id: str):
        return await self._policies.create(payload, subject_ref=account_id)

    async def evaluate_quote(
        self,
        payload: PolicyEvaluationCreate,
        *,
        account_id: str,
        legacy_subject: str,
    ) -> McpEvaluationResult:
        evaluation = await self._evaluations.create(
            payload,
            owned_subject_refs=frozenset((account_id, legacy_subject)),
        )
        allowed = PolicyDecision(evaluation.decision) is PolicyDecision.ALLOW
        return McpEvaluationResult(
            evaluation=evaluation,
            state="human_approval_required" if allowed else "denied",
            reason_code=(
                "MCP_HUMAN_APPROVAL_REQUIRED"
                if allowed
                else next(iter(evaluation.reason_codes), "POLICY_DENIED")
            ),
            approval_url=(self._purchase_url(evaluation.id) if allowed else None),
        )

    async def purchase_status(
        self,
        evaluation_id: str,
        *,
        account_id: str,
        legacy_subject: str,
    ) -> McpPurchaseStatusResponse:
        evaluation = await self._evaluations.get(
            evaluation_id,
            owned_subject_refs=frozenset((account_id, legacy_subject)),
        )
        quote = await self._quotes.get(evaluation.quote_id)
        if PolicyDecision(evaluation.decision) is PolicyDecision.DENY:
            return self._status(
                evaluation_id,
                "evaluation_denied",
                next(iter(evaluation.reason_codes), "POLICY_DENIED"),
            )
        if quote.state == "expired":
            return self._status(evaluation_id, "evaluation_denied", "QUOTE_EXPIRED")
        authorization = await self._session.scalar(
            select(PurchaseAuthorization)
            .where(PurchaseAuthorization.evaluation_id == evaluation_id)
            .order_by(PurchaseAuthorization.authorized_at.desc())
        )
        if authorization is None:
            return self._status(
                evaluation_id,
                "human_approval_required",
                "MCP_HUMAN_APPROVAL_REQUIRED",
                retry_after=5,
            )
        transaction = await self._session.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.authorization_id == authorization.id,
                PaymentTransaction.account_id == account_id,
            )
        )
        if transaction is None:
            return self._status(
                evaluation_id,
                "authorized",
                "PURCHASE_AUTHORIZED",
                authorization_id=authorization.id,
            )
        payment = await self._payment.get_transaction(transaction.id, account_id=account_id)
        common = {
            "authorization_id": authorization.id,
            "transaction_id": transaction.id,
        }
        if payment.commerce_outcome == "refunded":
            return self._status(evaluation_id, "refunded", "MCP_COMMERCE_REFUNDED", **common)
        if payment.commerce_outcome == "manual_review":
            return self._status(evaluation_id, "manual_review", "COMMERCE_MANUAL_REVIEW", **common)
        if payment.commerce_outcome == "compensation_pending":
            return self._status(
                evaluation_id,
                "compensation_pending",
                "COMMERCE_COMPENSATION_PENDING",
                retry_after=10,
                **common,
            )
        if payment.state.value != "paid":
            failed = bool(payment.attempts) and all(
                attempt.status.value == "failed" for attempt in payment.attempts
            )
            return self._status(
                evaluation_id,
                "payment_failed"
                if failed
                else "payment_ready"
                if payment.checkout
                else "payment_pending",
                "PAYMENT_ATTEMPT_FAILED" if failed else "MCP_PAYMENT_PENDING",
                retry_after=5,
                **common,
            )
        if self._entitlements is None:
            return self._status(
                evaluation_id,
                "paid",
                "FULFILLMENT_PROVIDER_UNAVAILABLE",
                retry_after=10,
                **common,
            )
        lookup = await self._entitlements.lookup_for_transaction(
            transaction.id, account_id=account_id
        )
        if lookup.entitlement is None:
            return self._status(
                evaluation_id,
                "entitlement_preparing",
                "MCP_ENTITLEMENT_PREPARING",
                retry_after=5,
                **common,
            )
        execution = await self._session.scalar(
            select(FulfillmentExecution).where(
                FulfillmentExecution.entitlement_id == lookup.entitlement.id
            )
        )
        entitlement_common = {**common, "entitlement_id": lookup.entitlement.id}
        if execution is None:
            return self._status(
                evaluation_id,
                "access_ready",
                "ENTITLEMENT_ACTIVE",
                **entitlement_common,
            )
        if (
            FulfillmentExecutionState(execution.execution_state)
            is FulfillmentExecutionState.SUCCEEDED
        ):
            return self._status(
                evaluation_id,
                "fulfilled",
                "FULFILLMENT_SUCCEEDED",
                execution_id=execution.id,
                result_hash=execution.result_hash,
                **entitlement_common,
            )
        return self._status(
            evaluation_id,
            "access_ready",
            execution.failure_code or "FULFILLMENT_PENDING",
            retry_after=5,
            execution_id=execution.id,
            **entitlement_common,
        )

    async def get_entitlement(self, transaction_id: str, *, account_id: str):
        service = self._require_entitlements()
        lookup = await service.lookup_for_transaction(transaction_id, account_id=account_id)
        response = None
        if lookup.entitlement is not None:
            response = self._entitlement_response(
                await service.get_view(lookup.entitlement.id, account_id=account_id)
            )
        timeline = await service.timeline_for_transaction(transaction_id, account_id=account_id)
        return EntitlementLookupResponse(
            transaction_id=transaction_id,
            state=lookup.state,
            reason_code=lookup.reason_code,
            entitlement=response,
            timeline=[
                EntitlementTimelineEvent(
                    event_type=item.event_type,
                    reason_code=item.reason_code,
                    occurred_at=item.occurred_at,
                )
                for item in timeline
            ],
        )

    async def request_capability(self, entitlement_id: str, *, account_id: str):
        issued = await self._require_entitlements().issue_capability(
            entitlement_id,
            account_id=account_id,
        )
        return CapabilityResponse(
            entitlement_id=issued.claims.entitlement_id,
            token=issued.token,
            expires_at=issued.claims.expires_at,
            maximum_executions=1,
        )

    async def execute_resource(
        self,
        *,
        merchant_slug: str,
        service_slug: str,
        input_value: object,
        capability: str,
    ) -> FulfillmentPendingResponse | FulfillmentResultResponse:
        if self._fulfillments is None:
            raise McpSessionConflictError(
                "Paid fulfillment is unavailable",
                "FULFILLMENT_PROVIDER_UNAVAILABLE",
            )
        operation = await self._fulfillments.execute(
            merchant_slug=merchant_slug,
            service_slug=service_slug,
            input_value=input_value,
            token=capability,
        )
        if operation.result is None:
            return FulfillmentPendingResponse(
                execution_id=operation.execution_id,
                entitlement_id=operation.entitlement_id,
                reason_code=(
                    "FULFILLMENT_PROVIDER_IN_PROGRESS"
                    if operation.reason_code == "FULFILLMENT_PROVIDER_IN_PROGRESS"
                    else "FULFILLMENT_ALREADY_CLAIMED"
                ),
            )
        result = operation.result
        return FulfillmentResultResponse(
            execution_id=result.execution_id,
            entitlement_id=result.entitlement_id,
            result_content_type=result.result_content_type,
            result=result.result,
            result_hash=result.result_hash,
            result_size_bytes=result.result_size_bytes,
            replayed_result=result.replayed_result,
            completed_at=result.completed_at,
        )

    def _require_entitlements(self) -> EntitlementApplicationService:
        if self._entitlements is None:
            raise McpSessionConflictError(
                "Paid entitlement access is unavailable",
                "FULFILLMENT_PROVIDER_UNAVAILABLE",
            )
        return self._entitlements

    def _status(
        self,
        evaluation_id: str,
        state: str,
        reason_code: str,
        *,
        retry_after: int | None = None,
        authorization_id: str | None = None,
        transaction_id: str | None = None,
        entitlement_id: str | None = None,
        execution_id: str | None = None,
        result_hash: str | None = None,
    ) -> McpPurchaseStatusResponse:
        handoff_url = self._purchase_url(evaluation_id)
        return McpPurchaseStatusResponse(
            evaluation_id=evaluation_id,
            state=state,  # type: ignore[arg-type]
            reason_code=reason_code,
            retry_after_seconds=retry_after,
            approval_url=handoff_url if state == "human_approval_required" else None,
            payment_url=(
                handoff_url
                if state in {"authorized", "payment_ready", "payment_pending", "payment_failed"}
                else None
            ),
            authorization_id=authorization_id,
            transaction_id=transaction_id,
            entitlement_id=entitlement_id,
            execution_id=execution_id,
            result_hash=result_hash,
        )

    def _purchase_url(self, evaluation_id: str) -> str:
        return f"{self._settings.mcp_frontend_base_url}/agent-purchases/{evaluation_id}"

    @staticmethod
    def _entitlement_response(view: EntitlementView) -> EntitlementResponse:
        entitlement: Entitlement = view.entitlement
        expires_at = McpAgentSessionService._utc(entitlement.expires_at)
        return EntitlementResponse(
            entitlement_id=entitlement.id,
            transaction_id=entitlement.transaction_id,
            merchant=ResourceParty(
                id=view.merchant.id, slug=view.merchant.slug, name=view.merchant.name
            ),
            service=ResourceParty(
                id=view.service.id, slug=view.service.slug, name=view.service.name
            ),
            input=entitlement.input,
            input_hash=entitlement.input_hash,
            amount=entitlement.amount,
            currency=entitlement.currency,
            purchase_type=entitlement.purchase_type,
            maximum_executions=1,
            issued_at=McpAgentSessionService._utc(entitlement.issued_at),
            expires_at=expires_at,
            state="active" if expires_at > datetime.now(UTC) else "expired",
            entitlement_version="1",
            entitlement_hash=entitlement.entitlement_hash,
        )
