"""Request-scoped application-service and authenticated-principal dependencies."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.approval_challenges import ChallengeStore
from app.cache.auth import AuthStore
from app.cache.human_presence import HumanPresenceStore
from app.cache.mcp import McpRateLimiter
from app.cache.operator import OperatorRateLimiter
from app.cache.payment_webhooks import WebhookQueuePublisher
from app.core.config import Settings
from app.db.session import get_session
from app.domain.enums import AccountStatus
from app.domain.exceptions import (
    AuthenticationForbiddenError,
    AuthenticationUnauthorizedError,
)
from app.models import OperatorRole
from app.providers import PaymentProvider, RefundProvider
from app.providers.fulfillment import FulfillmentProvider, UnavailableFulfillmentProvider
from app.repositories.accounts import AccountRepository
from app.repositories.approval_identities import ApprovalIdentityRepository
from app.repositories.authorizations import PurchaseAuthorizationRepository
from app.repositories.buyer_policies import BuyerPolicyRepository
from app.repositories.merchants import MerchantRepository
from app.repositories.passkey_credentials import PasskeyCredentialRepository
from app.repositories.policy_evaluations import PolicyEvaluationRepository
from app.repositories.quotes import QuoteRepository
from app.repositories.services import ServiceRepository
from app.services.approvals import ApprovalApplicationService
from app.services.auth import AuthenticationApplicationService, ResolvedAuthSession
from app.services.capabilities import CapabilityTokenService
from app.services.catalog import CatalogApplicationService
from app.services.compensations import CompensationApplicationService, RefundApplicationService
from app.services.entitlements import EntitlementApplicationService
from app.services.fulfillments import FulfillmentApplicationService
from app.services.human_presence import HumanPresenceService
from app.services.mcp import McpAgentSessionService, McpAuditService, McpCommerceService
from app.services.merchants import MerchantApplicationService
from app.services.passkeys import PasskeyApplicationService
from app.services.payment_webhooks import RazorpayWebhookIngressService
from app.services.payments import PaymentApplicationService
from app.services.policies import BuyerPolicyApplicationService
from app.services.policy_evaluations import PolicyEvaluationApplicationService
from app.services.quotes import QuoteApplicationService
from app.services.services import ServiceApplicationService
from app.services.webauthn import WebAuthnBackend

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def get_application_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError("Application settings are not configured")
    return settings


SettingsDependency = Annotated[Settings, Depends(get_application_settings)]


def get_challenge_store(request: Request) -> ChallengeStore:
    challenge_store = getattr(request.app.state, "challenge_store", None)
    if not isinstance(challenge_store, ChallengeStore):
        raise RuntimeError("Approval challenge storage is not configured")
    return challenge_store


def get_webauthn_backend(request: Request) -> WebAuthnBackend:
    webauthn_backend = getattr(request.app.state, "webauthn_backend", None)
    if not isinstance(webauthn_backend, WebAuthnBackend):
        raise RuntimeError("WebAuthn backend is not configured")
    return webauthn_backend


ChallengeStoreDependency = Annotated[ChallengeStore, Depends(get_challenge_store)]
WebAuthnBackendDependency = Annotated[WebAuthnBackend, Depends(get_webauthn_backend)]


def get_auth_store(request: Request) -> AuthStore:
    auth_store = getattr(request.app.state, "auth_store", None)
    if not isinstance(auth_store, AuthStore):
        raise RuntimeError("Authentication storage is not configured")
    return auth_store


AuthStoreDependency = Annotated[AuthStore, Depends(get_auth_store)]


def get_human_presence_store(request: Request) -> HumanPresenceStore:
    store = getattr(request.app.state, "human_presence_store", None)
    if not isinstance(store, HumanPresenceStore):
        raise RuntimeError("Human-presence challenge storage is unavailable")
    return store


HumanPresenceStoreDependency = Annotated[
    HumanPresenceStore,
    Depends(get_human_presence_store),
]


def get_operator_rate_limiter(request: Request) -> OperatorRateLimiter:
    limiter = getattr(request.app.state, "operator_rate_limiter", None)
    if not isinstance(limiter, OperatorRateLimiter):
        raise RuntimeError("Operator rate limiting is unavailable")
    return limiter


OperatorRateLimiterDependency = Annotated[
    OperatorRateLimiter,
    Depends(get_operator_rate_limiter),
]


def get_mcp_rate_limiter(request: Request) -> McpRateLimiter:
    limiter = getattr(request.app.state, "mcp_rate_limiter", None)
    if not isinstance(limiter, McpRateLimiter):
        raise RuntimeError("MCP rate limiting is unavailable")
    return limiter


McpRateLimiterDependency = Annotated[
    McpRateLimiter,
    Depends(get_mcp_rate_limiter),
]


def get_payment_provider(request: Request) -> PaymentProvider | None:
    provider = getattr(request.app.state, "payment_provider", None)
    if provider is not None and not isinstance(provider, PaymentProvider):
        raise RuntimeError("Payment provider is not configured correctly")
    return provider


def get_payment_webhook_queue(request: Request) -> WebhookQueuePublisher | None:
    queue = getattr(request.app.state, "payment_webhook_queue", None)
    if queue is not None and not isinstance(queue, WebhookQueuePublisher):
        raise RuntimeError("Payment webhook queue is not configured correctly")
    return queue


PaymentProviderDependency = Annotated[
    PaymentProvider | None,
    Depends(get_payment_provider),
]
PaymentWebhookQueueDependency = Annotated[
    WebhookQueuePublisher | None,
    Depends(get_payment_webhook_queue),
]


def get_fulfillment_provider(request: Request) -> FulfillmentProvider | None:
    provider = getattr(request.app.state, "fulfillment_provider", None)
    if provider is not None and not isinstance(provider, FulfillmentProvider):
        raise RuntimeError("Fulfillment provider is not configured correctly")
    return provider


FulfillmentProviderDependency = Annotated[
    FulfillmentProvider | None,
    Depends(get_fulfillment_provider),
]


def get_merchant_application_service(
    session: SessionDependency,
) -> MerchantApplicationService:
    return MerchantApplicationService(MerchantRepository(session))


def get_service_application_service(
    session: SessionDependency,
) -> ServiceApplicationService:
    return ServiceApplicationService(
        MerchantRepository(session),
        ServiceRepository(session),
    )


def get_catalog_application_service(
    session: SessionDependency,
) -> CatalogApplicationService:
    return CatalogApplicationService(ServiceRepository(session))


def get_quote_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
) -> QuoteApplicationService:
    return QuoteApplicationService(
        ServiceRepository(session),
        QuoteRepository(session),
        ttl=timedelta(seconds=settings.quote_ttl_seconds),
    )


def get_buyer_policy_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
) -> BuyerPolicyApplicationService:
    return BuyerPolicyApplicationService(
        BuyerPolicyRepository(session),
        maximum_ttl=timedelta(seconds=settings.policy_max_ttl_seconds),
    )


def get_policy_evaluation_application_service(
    session: SessionDependency,
) -> PolicyEvaluationApplicationService:
    return PolicyEvaluationApplicationService(
        BuyerPolicyRepository(session),
        QuoteRepository(session),
        PolicyEvaluationRepository(session),
    )


def get_passkey_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
    challenge_store: ChallengeStoreDependency,
    webauthn_backend: WebAuthnBackendDependency,
) -> PasskeyApplicationService:
    return PasskeyApplicationService(
        AccountRepository(session),
        ApprovalIdentityRepository(session),
        PasskeyCredentialRepository(session),
        challenge_store,
        webauthn_backend,
        challenge_ttl=timedelta(seconds=settings.webauthn_challenge_ttl_seconds),
    )


def get_approval_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
    challenge_store: ChallengeStoreDependency,
    webauthn_backend: WebAuthnBackendDependency,
) -> ApprovalApplicationService:
    return ApprovalApplicationService(
        AccountRepository(session),
        ApprovalIdentityRepository(session),
        PasskeyCredentialRepository(session),
        PurchaseAuthorizationRepository(session),
        BuyerPolicyRepository(session),
        QuoteRepository(session),
        PolicyEvaluationRepository(session),
        challenge_store,
        webauthn_backend,
        challenge_ttl=timedelta(seconds=settings.webauthn_challenge_ttl_seconds),
        authorization_ttl=timedelta(seconds=settings.authorization_ttl_seconds),
    )


def get_authentication_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
    auth_store: AuthStoreDependency,
    webauthn_backend: WebAuthnBackendDependency,
) -> AuthenticationApplicationService:
    return AuthenticationApplicationService(
        AccountRepository(session),
        ApprovalIdentityRepository(session),
        PasskeyCredentialRepository(session),
        auth_store,
        webauthn_backend,
        challenge_ttl=timedelta(seconds=settings.webauthn_challenge_ttl_seconds),
        session_ttl=timedelta(seconds=settings.auth_session_ttl_seconds),
    )


def get_payment_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
    provider: PaymentProviderDependency,
) -> PaymentApplicationService:
    return PaymentApplicationService(session, provider, settings)


def get_compensation_application_service(
    session: SessionDependency,
) -> CompensationApplicationService:
    return CompensationApplicationService(session)


def get_refund_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
    provider: PaymentProviderDependency,
) -> RefundApplicationService:
    refund_provider = provider if isinstance(provider, RefundProvider) else None
    return RefundApplicationService(session, refund_provider, settings)


def _capability_token_service(settings: Settings) -> CapabilityTokenService:
    if not settings.fulfillment_enabled or settings.entitlement_token_secret is None:
        raise RuntimeError("Paid fulfillment is disabled")
    return CapabilityTokenService(
        settings.entitlement_token_secret.get_secret_value(),
        ttl=timedelta(seconds=settings.entitlement_token_ttl_seconds),
    )


def get_entitlement_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
    payment_provider: PaymentProviderDependency,
) -> EntitlementApplicationService:
    payment_service = PaymentApplicationService(session, payment_provider, settings)
    return EntitlementApplicationService(
        session,
        payment_service,
        _capability_token_service(settings),
        entitlement_ttl=timedelta(seconds=settings.entitlement_ttl_seconds),
    )


def get_fulfillment_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
    provider: FulfillmentProviderDependency,
    payment_provider: PaymentProviderDependency,
) -> FulfillmentApplicationService:
    if provider is None:
        provider = UnavailableFulfillmentProvider()
    return FulfillmentApplicationService(
        session,
        _capability_token_service(settings),
        provider,
        payment_eligibility=PaymentApplicationService(session, payment_provider, settings),
        provider_base_url=settings.orbitintel_base_url,
        execution_lease=timedelta(seconds=settings.fulfillment_execution_lease_seconds),
        maximum_result_bytes=settings.fulfillment_max_result_bytes,
        default_maximum_attempts=settings.fulfillment_max_attempts,
    )


def get_mcp_agent_session_service(
    session: SessionDependency,
    settings: SettingsDependency,
) -> McpAgentSessionService:
    return McpAgentSessionService(session, settings)


def get_human_presence_service(
    session: SessionDependency,
    settings: SettingsDependency,
    store: HumanPresenceStoreDependency,
    webauthn: WebAuthnBackendDependency,
) -> HumanPresenceService:
    return HumanPresenceService(session, store, webauthn, settings)


def get_mcp_audit_service(session: SessionDependency) -> McpAuditService:
    return McpAuditService(session)


def get_mcp_commerce_service(
    session: SessionDependency,
    settings: SettingsDependency,
    payment_provider: PaymentProviderDependency,
    fulfillment_provider: FulfillmentProviderDependency,
) -> McpCommerceService:
    return McpCommerceService(
        session,
        settings,
        payment_provider,
        fulfillment_provider,
    )


def get_razorpay_webhook_ingress_service(
    settings: SettingsDependency,
    queue: PaymentWebhookQueueDependency,
) -> RazorpayWebhookIngressService:
    secret = (
        settings.razorpay_webhook_secret.get_secret_value()
        if settings.payments_enabled and settings.razorpay_webhook_secret is not None
        else None
    )
    previous_secret = (
        settings.razorpay_previous_webhook_secret.get_secret_value()
        if settings.payments_enabled and settings.razorpay_previous_webhook_secret is not None
        else None
    )
    return RazorpayWebhookIngressService(
        queue if settings.payments_enabled else None,
        webhook_secret=secret,
        previous_webhook_secret=previous_secret,
        maximum_age=timedelta(seconds=settings.razorpay_webhook_max_age_seconds),
    )


AuthenticationApplicationDependency = Annotated[
    AuthenticationApplicationService,
    Depends(get_authentication_application_service),
]


def _single_header(request: Request, name: bytes) -> str | None:
    """Return one raw header value, rejecting ambiguous duplicate evidence."""
    values = [value for key, value in request.scope.get("headers", ()) if key.lower() == name]
    if not values:
        return None
    if len(values) != 1:
        return ""
    try:
        return values[0].decode("latin-1")
    except UnicodeDecodeError:
        return ""


def require_allowed_origin(
    request: Request,
    settings: SettingsDependency,
) -> str:
    """Require exactly one configured WebAuthn-capable browser Origin."""
    origin = _single_header(request, b"origin")
    origin_allowed = (
        origin is not None
        and origin != ""
        and any(
            len(expected) == len(origin) and compare_digest(expected, origin)
            for expected in settings.webauthn_expected_origins
        )
    )
    if not origin_allowed:
        raise AuthenticationForbiddenError(
            "Request Origin is not allowed",
            "AUTH_ORIGIN_NOT_ALLOWED",
        )
    return origin


AllowedOriginDependency = Annotated[str, Depends(require_allowed_origin)]


def require_safe_authenticated_origin(
    request: Request,
    settings: SettingsDependency,
) -> str | None:
    """Block credentialed cross-origin reads outside the trusted browser origins."""
    origin = _single_header(request, b"origin")
    if origin is None:
        return None
    origin_allowed = origin != "" and any(
        len(expected) == len(origin) and compare_digest(expected, origin)
        for expected in settings.webauthn_expected_origins
    )
    if not origin_allowed:
        raise AuthenticationForbiddenError(
            "Request Origin is not allowed",
            "AUTH_ORIGIN_NOT_ALLOWED",
        )
    return origin


AuthenticatedOriginDependency = Annotated[
    str | None,
    Depends(require_safe_authenticated_origin),
]


def disable_private_caching(response: Response) -> None:
    """Keep cookie-authenticated and ceremony responses out of shared caches."""
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"


async def require_current_account(
    request: Request,
    response: Response,
    settings: SettingsDependency,
    application_service: AuthenticationApplicationDependency,
    authenticated_origin: AuthenticatedOriginDependency,
) -> ResolvedAuthSession:
    """Resolve the opaque cookie into an active account on every protected request."""
    del authenticated_origin
    disable_private_caching(response)
    session_id = request.cookies.get(settings.auth_cookie_name)
    if not session_id:
        raise AuthenticationUnauthorizedError(
            "An authenticated session is required",
            "AUTH_SESSION_REQUIRED",
        )
    return await application_service.resolve_session(session_id)


CurrentAccountDependency = Annotated[
    ResolvedAuthSession,
    Depends(require_current_account),
]


async def require_authenticated_mutation(
    request: Request,
    settings: SettingsDependency,
    session: SessionDependency,
    current: CurrentAccountDependency,
    origin: AllowedOriginDependency,
) -> ResolvedAuthSession:
    """Apply strict Origin and synchronizer-token checks to cookie mutations."""
    del origin
    csrf_token = _single_header(request, b"x-csrf-token")
    if csrf_token is None:
        raise AuthenticationForbiddenError(
            "A CSRF token is required",
            "AUTH_CSRF_REQUIRED",
        )
    expected = current.state.csrf_token
    if len(csrf_token) != len(expected) or not compare_digest(csrf_token, expected):
        raise AuthenticationForbiddenError(
            "The CSRF token is invalid",
            "AUTH_CSRF_INVALID",
        )
    account = await AccountRepository(session).get_for_update(current.account.id)
    if account is None or account.session_version != current.state.account_session_version:
        raise AuthenticationUnauthorizedError(
            "Authentication session is invalid",
            "AUTH_SESSION_INVALID",
        )
    status = AccountStatus(account.status)
    if status is AccountStatus.DISABLED:
        raise AuthenticationUnauthorizedError(
            "Account is disabled",
            "AUTH_ACCOUNT_DISABLED",
        )
    if status is not AccountStatus.ACTIVE:
        raise AuthenticationUnauthorizedError(
            "Account is pending first-passkey verification",
            "AUTH_ACCOUNT_PENDING",
        )
    return current


AuthenticatedMutationDependency = Annotated[
    ResolvedAuthSession,
    Depends(require_authenticated_mutation),
]


async def require_recent_authentication(
    settings: SettingsDependency,
    current: AuthenticatedMutationDependency,
) -> ResolvedAuthSession:
    """Require the session's passkey proof to fall inside the reauthentication window."""
    authenticated_at = current.state.authenticated_at.astimezone(UTC)
    if datetime.now(UTC) - authenticated_at > timedelta(
        seconds=settings.auth_reauth_max_age_seconds
    ):
        raise AuthenticationForbiddenError(
            "Recent passkey authentication is required",
            "AUTH_REAUTH_REQUIRED",
        )
    return current


RecentAuthenticationDependency = Annotated[
    ResolvedAuthSession,
    Depends(require_recent_authentication),
]


@dataclass(frozen=True, slots=True)
class OperatorContext:
    account_id: str
    role: str


async def require_operator(
    current: CurrentAccountDependency,
    session: SessionDependency,
) -> OperatorContext:
    assignment = await session.scalar(
        select(OperatorRole).where(
            OperatorRole.account_id == current.account.id,
            OperatorRole.status == "active",
        )
    )
    if assignment is None:
        raise AuthenticationForbiddenError(
            "An active operator role is required", "OPERATOR_ROLE_REQUIRED"
        )
    return OperatorContext(account_id=current.account.id, role=assignment.role)


OperatorDependency = Annotated[OperatorContext, Depends(require_operator)]


async def require_recent_operator(
    current: RecentAuthenticationDependency,
    session: SessionDependency,
    settings: SettingsDependency,
) -> OperatorContext:
    if datetime.now(UTC) - current.state.authenticated_at.astimezone(UTC) > timedelta(
        seconds=settings.operator_reauth_max_age_seconds
    ):
        raise AuthenticationForbiddenError(
            "Recent operator passkey authentication is required",
            "OPERATOR_REAUTH_REQUIRED",
        )
    assignment = await session.scalar(
        select(OperatorRole).where(
            OperatorRole.account_id == current.account.id,
            OperatorRole.status == "active",
        )
    )
    if assignment is None:
        raise AuthenticationForbiddenError(
            "An active operator role is required", "OPERATOR_ROLE_REQUIRED"
        )
    return OperatorContext(account_id=current.account.id, role=assignment.role)


RecentOperatorDependency = Annotated[OperatorContext, Depends(require_recent_operator)]


async def require_catalog_admin(operator: RecentOperatorDependency) -> OperatorContext:
    """Catalog terms are administration, not buyer or routine operator authority."""
    if operator.role != "admin":
        raise AuthenticationForbiddenError(
            "An active administrator role is required to change catalog terms",
            "CATALOG_ADMIN_REQUIRED",
        )
    return operator


MerchantApplicationDependency = Annotated[
    MerchantApplicationService,
    Depends(get_merchant_application_service),
]
ServiceApplicationDependency = Annotated[
    ServiceApplicationService,
    Depends(get_service_application_service),
]
CatalogApplicationDependency = Annotated[
    CatalogApplicationService,
    Depends(get_catalog_application_service),
]
QuoteApplicationDependency = Annotated[
    QuoteApplicationService,
    Depends(get_quote_application_service),
]
BuyerPolicyApplicationDependency = Annotated[
    BuyerPolicyApplicationService,
    Depends(get_buyer_policy_application_service),
]
PolicyEvaluationApplicationDependency = Annotated[
    PolicyEvaluationApplicationService,
    Depends(get_policy_evaluation_application_service),
]
PasskeyApplicationDependency = Annotated[
    PasskeyApplicationService,
    Depends(get_passkey_application_service),
]
ApprovalApplicationDependency = Annotated[
    ApprovalApplicationService,
    Depends(get_approval_application_service),
]
PaymentApplicationDependency = Annotated[
    PaymentApplicationService,
    Depends(get_payment_application_service),
]
CompensationApplicationDependency = Annotated[
    CompensationApplicationService,
    Depends(get_compensation_application_service),
]
RefundApplicationDependency = Annotated[
    RefundApplicationService,
    Depends(get_refund_application_service),
]
EntitlementApplicationDependency = Annotated[
    EntitlementApplicationService,
    Depends(get_entitlement_application_service),
]
FulfillmentApplicationDependency = Annotated[
    FulfillmentApplicationService,
    Depends(get_fulfillment_application_service),
]
McpAgentSessionServiceDependency = Annotated[
    McpAgentSessionService,
    Depends(get_mcp_agent_session_service),
]
HumanPresenceServiceDependency = Annotated[
    HumanPresenceService,
    Depends(get_human_presence_service),
]
McpAuditServiceDependency = Annotated[
    McpAuditService,
    Depends(get_mcp_audit_service),
]
McpCommerceServiceDependency = Annotated[
    McpCommerceService,
    Depends(get_mcp_commerce_service),
]
RazorpayWebhookIngressDependency = Annotated[
    RazorpayWebhookIngressService,
    Depends(get_razorpay_webhook_ingress_service),
]
