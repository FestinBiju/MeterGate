"""Scoped buyer-agent session management and narrow MCP commerce endpoints."""

import re
import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.v1.dependencies import (
    AllowedOriginDependency,
    AuthenticatedMutationDependency,
    CurrentAccountDependency,
    HumanPresenceServiceDependency,
    McpAgentSessionServiceDependency,
    McpAuditServiceDependency,
    McpCommerceServiceDependency,
    McpRateLimiterDependency,
    SessionDependency,
    SettingsDependency,
    _single_header,
)
from app.cache.mcp import McpRateLimiterUnavailable, McpRateLimitExceeded
from app.domain.exceptions import DomainError, McpSessionUnauthorizedError
from app.schemas.mcp import (
    McpAgentSessionCreate,
    McpAgentSessionCreated,
    McpAgentSessionId,
    McpAgentSessionList,
    McpAgentSessionRenew,
    McpAgentSessionResponse,
    McpCapabilityInput,
    McpEmptyInput,
    McpEntitlementInput,
    McpEvaluateQuoteInput,
    McpEvaluationResult,
    McpExecuteResourceInput,
    McpGetServiceInput,
    McpPolicyInput,
    McpPurchaseStatusInput,
    McpPurchaseStatusResponse,
    McpQuoteInput,
    McpResourceInput,
    McpScope,
)
from app.schemas.policies import BuyerPolicyResponse
from app.schemas.policy_evaluations import PolicyEvaluationCreate
from app.schemas.quotes import QuoteResponse
from app.services.mcp import ResolvedMcpSession

router = APIRouter(prefix="/mcp", tags=["agent MCP bridge"])
_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


@router.post(
    "/sessions",
    response_model=McpAgentSessionCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_session(
    payload: McpAgentSessionCreate,
    response: Response,
    service: McpAgentSessionServiceDependency,
    current: AuthenticatedMutationDependency,
    origin: AllowedOriginDependency,
    human_presence: HumanPresenceServiceDependency,
) -> McpAgentSessionCreated:
    await human_presence.require_and_consume(
        payload.human_presence_proof_id,
        account_id=current.account.id,
        session_id=current.state.session_id,
        origin=origin,
        action_class="new_agent_session",
        resource_binding={
            "scopes": list(payload.scopes),
            "expires_in_seconds": payload.expires_in_seconds,
        },
    )
    created = await service.create(payload, account_id=current.account.id)
    _no_store(response)
    return created


@router.get("/sessions", response_model=McpAgentSessionList)
async def list_agent_sessions(
    response: Response,
    service: McpAgentSessionServiceDependency,
    current: CurrentAccountDependency,
) -> McpAgentSessionList:
    _no_store(response)
    return McpAgentSessionList(sessions=await service.list(account_id=current.account.id))


@router.post(
    "/sessions/{agent_session_id}/revoke",
    response_model=McpAgentSessionResponse,
)
async def revoke_agent_session(
    agent_session_id: McpAgentSessionId,
    response: Response,
    service: McpAgentSessionServiceDependency,
    current: AuthenticatedMutationDependency,
) -> McpAgentSessionResponse:
    revoked = await service.revoke(agent_session_id, account_id=current.account.id)
    _no_store(response)
    return revoked


@router.post("/sessions/{agent_session_id}/renew", response_model=McpAgentSessionResponse)
async def renew_agent_session(
    agent_session_id: McpAgentSessionId,
    payload: McpAgentSessionRenew,
    response: Response,
    service: McpAgentSessionServiceDependency,
    current: AuthenticatedMutationDependency,
    origin: AllowedOriginDependency,
    human_presence: HumanPresenceServiceDependency,
) -> McpAgentSessionResponse:
    record = await service.get_renewable(agent_session_id, account_id=current.account.id)
    await human_presence.require_and_consume(
        payload.human_presence_proof_id,
        account_id=current.account.id,
        session_id=current.state.session_id,
        origin=origin,
        action_class="renew_agent_session",
        resource_binding={
            "agent_session_id": record.id,
            "scopes": list(record.scopes),
            "expires_in_seconds": payload.expires_in_seconds,
        },
    )
    renewed = await service.renew(record, payload)
    _no_store(response)
    return renewed


@router.post("/tools/list-services")
async def list_services(
    payload: McpEmptyInput,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
):
    del payload
    return await _invoke(
        request,
        response,
        session=session,
        settings=settings,
        agent_sessions=agent_sessions,
        commerce=commerce,
        audit=audit,
        limiter=limiter,
        scope="mcp:catalog.read",
        tool_name="list_services",
        rate_class="catalog",
        resource_ids={},
        success_code="MCP_SERVICES_LISTED",
        call=lambda _: commerce.list_services(),
    )


@router.post("/tools/get-service")
async def get_service(
    payload: McpGetServiceInput,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
):
    return await _invoke(
        request,
        response,
        session=session,
        settings=settings,
        agent_sessions=agent_sessions,
        commerce=commerce,
        audit=audit,
        limiter=limiter,
        scope="mcp:catalog.read",
        tool_name="get_service",
        rate_class="catalog",
        resource_ids={"service_id": payload.service_id},
        success_code="MCP_SERVICE_RETURNED",
        call=lambda _: commerce.get_service(payload.service_id),
    )


@router.post("/tools/inspect-payment-requirement")
async def inspect_payment_requirement(
    payload: McpResourceInput,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
):
    return await _invoke(
        request,
        response,
        session=session,
        settings=settings,
        agent_sessions=agent_sessions,
        commerce=commerce,
        audit=audit,
        limiter=limiter,
        scope="mcp:catalog.read",
        tool_name="inspect_payment_requirement",
        rate_class="catalog",
        resource_ids={
            "merchant_slug": payload.merchant_slug,
            "service_slug": payload.service_slug,
        },
        success_code="MCP_PAYMENT_REQUIREMENT_RETURNED",
        call=lambda _: commerce.inspect_payment_requirement(
            payload.merchant_slug,
            payload.service_slug,
            payload.input,
        ),
    )


@router.post("/tools/request-quote", response_model=QuoteResponse)
async def request_quote(
    payload: McpQuoteInput,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
):
    return await _invoke(
        request,
        response,
        session=session,
        settings=settings,
        agent_sessions=agent_sessions,
        commerce=commerce,
        audit=audit,
        limiter=limiter,
        scope="mcp:quote.create",
        tool_name="request_quote",
        rate_class="mutation",
        resource_ids={"service_id": payload.service_id},
        success_code="QUOTE_CREATED",
        call=lambda _: commerce.request_quote(payload),
    )


@router.post("/tools/create-buyer-policy", response_model=BuyerPolicyResponse)
async def create_buyer_policy(
    payload: McpPolicyInput,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
):
    return await _invoke(
        request,
        response,
        session=session,
        settings=settings,
        agent_sessions=agent_sessions,
        commerce=commerce,
        audit=audit,
        limiter=limiter,
        scope="mcp:policy.create",
        tool_name="create_buyer_policy",
        rate_class="mutation",
        resource_ids={},
        success_code="POLICY_CREATED",
        call=lambda principal: commerce.create_policy(payload, account_id=principal.account_id),
    )


@router.post("/tools/evaluate-quote", response_model=McpEvaluationResult)
async def evaluate_quote(
    payload: McpEvaluateQuoteInput,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
):
    return await _invoke(
        request,
        response,
        session=session,
        settings=settings,
        agent_sessions=agent_sessions,
        commerce=commerce,
        audit=audit,
        limiter=limiter,
        scope="mcp:policy.evaluate",
        tool_name="evaluate_quote",
        rate_class="mutation",
        resource_ids={"policy_id": payload.policy_id, "quote_id": payload.quote_id},
        success_code="POLICY_EVALUATED",
        call=lambda principal: commerce.evaluate_quote(
            PolicyEvaluationCreate(policy_id=payload.policy_id, quote_id=payload.quote_id),
            account_id=principal.account_id,
            legacy_subject=principal.approval_subject_ref,
        ),
    )


@router.post("/tools/get-purchase-status", response_model=McpPurchaseStatusResponse)
async def get_purchase_status(
    payload: McpPurchaseStatusInput,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
):
    return await _invoke(
        request,
        response,
        session=session,
        settings=settings,
        agent_sessions=agent_sessions,
        commerce=commerce,
        audit=audit,
        limiter=limiter,
        scope="mcp:commerce.read",
        tool_name="get_purchase_status",
        rate_class="status",
        resource_ids={"evaluation_id": payload.evaluation_id},
        success_code="MCP_PURCHASE_STATUS_RETURNED",
        call=lambda principal: commerce.purchase_status(
            payload.evaluation_id,
            account_id=principal.account_id,
            legacy_subject=principal.approval_subject_ref,
        ),
    )


@router.post("/tools/get-entitlement")
async def get_entitlement(
    payload: McpEntitlementInput,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
):
    return await _invoke(
        request,
        response,
        session=session,
        settings=settings,
        agent_sessions=agent_sessions,
        commerce=commerce,
        audit=audit,
        limiter=limiter,
        scope="mcp:commerce.read",
        tool_name="get_entitlement",
        rate_class="status",
        resource_ids={"transaction_id": payload.transaction_id},
        success_code="MCP_ENTITLEMENT_RETURNED",
        call=lambda principal: commerce.get_entitlement(
            payload.transaction_id,
            account_id=principal.account_id,
        ),
    )


@router.post("/tools/request-capability")
async def request_capability(
    payload: McpCapabilityInput,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
):
    return await _invoke(
        request,
        response,
        session=session,
        settings=settings,
        agent_sessions=agent_sessions,
        commerce=commerce,
        audit=audit,
        limiter=limiter,
        scope="mcp:capability.issue",
        tool_name="request_capability",
        rate_class="mutation",
        resource_ids={"entitlement_id": payload.entitlement_id},
        success_code="CAPABILITY_ISSUED",
        call=lambda principal: commerce.request_capability(
            payload.entitlement_id,
            account_id=principal.account_id,
        ),
    )


@router.post("/tools/execute-paid-resource")
async def execute_paid_resource(
    payload: McpExecuteResourceInput,
    request: Request,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
):
    return await _invoke(
        request,
        response,
        session=session,
        settings=settings,
        agent_sessions=agent_sessions,
        commerce=commerce,
        audit=audit,
        limiter=limiter,
        scope="mcp:resource.execute",
        tool_name="execute_paid_resource",
        rate_class="mutation",
        resource_ids={
            "merchant_slug": payload.merchant_slug,
            "service_slug": payload.service_slug,
        },
        success_code="MCP_RESOURCE_EXECUTED",
        call=lambda _: commerce.execute_resource(
            merchant_slug=payload.merchant_slug,
            service_slug=payload.service_slug,
            input_value=payload.input,
            capability=payload.capability,
        ),
    )


async def _invoke[T](
    request: Request,
    response: Response,
    *,
    session: SessionDependency,
    settings: SettingsDependency,
    agent_sessions: McpAgentSessionServiceDependency,
    commerce: McpCommerceServiceDependency,
    audit: McpAuditServiceDependency,
    limiter: McpRateLimiterDependency,
    scope: McpScope,
    tool_name: str,
    rate_class: str,
    resource_ids: dict[str, str | None],
    success_code: str,
    call: Callable[[ResolvedMcpSession], Awaitable[T]],
) -> T:
    del commerce
    token = _bearer_token(request)
    principal = await agent_sessions.resolve(token, required_scope=scope)
    correlation_id = _correlation_id(request)
    try:
        limit = (
            settings.mcp_catalog_rate_limit
            if rate_class == "catalog"
            else settings.mcp_status_rate_limit
            if rate_class == "status"
            else settings.mcp_mutation_rate_limit
        )
        await limiter.require(
            account_id=principal.account_id,
            agent_session_id=principal.session.id,
            action=tool_name,
            limit=limit,
            window_seconds=settings.mcp_rate_limit_window_seconds,
        )
        result = await call(principal)
    except McpRateLimitExceeded as error:
        await session.rollback()
        await audit.record(
            principal,
            tool_name=tool_name,
            correlation_id=correlation_id,
            resource_ids=resource_ids,
            result_code="MCP_RATE_LIMITED",
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "MCP_RATE_LIMITED"},
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error
    except McpRateLimiterUnavailable as error:
        await session.rollback()
        await audit.record(
            principal,
            tool_name=tool_name,
            correlation_id=correlation_id,
            resource_ids=resource_ids,
            result_code="MCP_RATE_LIMIT_UNAVAILABLE",
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "MCP_RATE_LIMIT_UNAVAILABLE"},
            headers={"Retry-After": "5"},
        ) from error
    except Exception as error:
        await session.rollback()
        await audit.record(
            principal,
            tool_name=tool_name,
            correlation_id=correlation_id,
            resource_ids=resource_ids,
            result_code=_error_code(error),
        )
        raise
    resource_ids = {**resource_ids, **_result_resource_ids(tool_name, result)}
    await audit.record(
        principal,
        tool_name=tool_name,
        correlation_id=correlation_id,
        resource_ids=resource_ids,
        result_code=success_code,
    )
    _no_store(response)
    return result


def _result_resource_ids(tool_name: str, result: object) -> dict[str, str]:
    """Add only public artifact IDs needed to attribute real demo outcomes to MCP."""
    id_key = {
        "request_quote": "quote_id",
        "create_buyer_policy": "policy_id",
        "evaluate_quote": "evaluation_id",
    }.get(tool_name)
    if id_key is None:
        return {}
    value = getattr(result, "id", None)
    return {id_key: value} if isinstance(value, str) else {}


def _bearer_token(request: Request) -> str:
    authorization = _single_header(request, b"authorization")
    if (
        authorization is None
        or not authorization.startswith("Bearer ")
        or authorization != authorization.strip()
        or " " in authorization[len("Bearer ") :]
    ):
        raise McpSessionUnauthorizedError(
            "A valid MeterGate agent session is required",
            "MCP_SESSION_REQUIRED",
        )
    return authorization[len("Bearer ") :]


def _correlation_id(request: Request) -> str:
    supplied = _single_header(request, b"x-mcp-correlation-id")
    if supplied is None:
        return f"mcp-{uuid.uuid4().hex}"
    if _CORRELATION_PATTERN.fullmatch(supplied) is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "MCP_CORRELATION_ID_INVALID"},
        )
    return supplied


def _error_code(error: Exception) -> str:
    code = getattr(error, "reason_code", None)
    if isinstance(code, str):
        return code
    if isinstance(error, HTTPException) and isinstance(error.detail, dict):
        detail_code = error.detail.get("code")
        if isinstance(detail_code, str):
            return detail_code
    return "MCP_TOOL_FAILED" if isinstance(error, DomainError) else "MCP_INTERNAL_ERROR"


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
