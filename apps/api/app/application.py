"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.api.errors import register_domain_exception_handlers
from app.api.health import router as health_router
from app.api.v1.router import router as api_v1_router
from app.cache.approval_challenges import ChallengeStore, RedisChallengeStore
from app.cache.auth import AuthStore, RedisAuthStore
from app.cache.human_presence import HumanPresenceStore, RedisHumanPresenceStore
from app.cache.mcp import McpRateLimiter, RedisMcpRateLimiter
from app.cache.operator import OperatorRateLimiter, RedisOperatorRateLimiter
from app.cache.payment_webhooks import RedisPaymentWebhookQueue, WebhookQueuePublisher
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import Database, create_database
from app.providers import PaymentProvider
from app.providers.fulfillment import FulfillmentProvider, HttpMerchantFulfillmentProvider
from app.services.payments import build_razorpay_payment_provider
from app.services.readiness import ReadinessService, build_readiness_service
from app.services.webauthn import PyWebAuthnBackend, WebAuthnBackend


def create_app(
    *,
    settings: Settings | None = None,
    readiness_service: ReadinessService | None = None,
    database: Database | None = None,
    challenge_store: ChallengeStore | None = None,
    auth_store: AuthStore | None = None,
    webauthn_backend: WebAuthnBackend | None = None,
    payment_provider: PaymentProvider | None = None,
    payment_webhook_queue: WebhookQueuePublisher | None = None,
    fulfillment_provider: FulfillmentProvider | None = None,
    operator_rate_limiter: OperatorRateLimiter | None = None,
    mcp_rate_limiter: McpRateLimiter | None = None,
    human_presence_store: HumanPresenceStore | None = None,
) -> FastAPI:
    """Build an application with injectable durable and ephemeral infrastructure."""
    effective_settings = settings or get_settings()
    owns_database = database is None
    effective_database = database or create_database(effective_settings)
    # Redis is also the fail-closed enforcement store for operator and
    # reconciliation rate limits. Client construction itself is lazy.
    redis_client: Redis | None = Redis.from_url(
        effective_settings.redis_url.get_secret_value(),
        decode_responses=False,
    )
    if challenge_store is None:
        assert redis_client is not None
        effective_challenge_store: ChallengeStore = RedisChallengeStore(redis_client)
    else:
        effective_challenge_store = challenge_store
    if auth_store is None:
        assert redis_client is not None
        effective_auth_store: AuthStore = RedisAuthStore(redis_client)
    else:
        effective_auth_store = auth_store
    effective_payment_provider = payment_provider or build_razorpay_payment_provider(
        effective_settings
    )
    fulfillment_http_client: httpx.AsyncClient | None = None
    if fulfillment_provider is not None:
        effective_fulfillment_provider: FulfillmentProvider | None = fulfillment_provider
    elif effective_settings.fulfillment_enabled:
        assert effective_settings.orbitintel_shared_secret is not None
        fulfillment_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=effective_settings.orbitintel_connect_timeout_seconds,
                read=effective_settings.orbitintel_read_timeout_seconds,
                write=effective_settings.orbitintel_read_timeout_seconds,
                pool=effective_settings.orbitintel_connect_timeout_seconds,
            ),
            follow_redirects=False,
        )
        effective_fulfillment_provider = HttpMerchantFulfillmentProvider(
            fulfillment_http_client,
            base_url=effective_settings.orbitintel_base_url,
            shared_secret=effective_settings.orbitintel_shared_secret.get_secret_value(),
            maximum_response_bytes=effective_settings.fulfillment_max_result_bytes,
        )
    else:
        effective_fulfillment_provider = None
    if payment_webhook_queue is not None:
        effective_payment_webhook_queue: WebhookQueuePublisher | None = payment_webhook_queue
    elif effective_settings.payments_enabled:
        assert redis_client is not None
        effective_payment_webhook_queue = RedisPaymentWebhookQueue(redis_client)
    else:
        effective_payment_webhook_queue = None
    effective_webauthn_backend = webauthn_backend or PyWebAuthnBackend(
        rp_id=effective_settings.webauthn_rp_id,
        rp_name=effective_settings.webauthn_rp_name,
        expected_origins=effective_settings.webauthn_expected_origins,
        timeout_ms=effective_settings.webauthn_challenge_ttl_seconds * 1_000,
    )
    configure_logging(effective_settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            try:
                try:
                    if fulfillment_http_client is not None:
                        await fulfillment_http_client.aclose()
                finally:
                    if redis_client is not None:
                        await redis_client.aclose()
            finally:
                if owns_database:
                    await effective_database.dispose()

    application = FastAPI(
        title="MeterGate API",
        version="0.1.0",
        description="Core API for MeterGate agent-commerce infrastructure.",
        lifespan=lifespan,
    )
    application.state.settings = effective_settings
    application.state.database = effective_database
    application.state.challenge_store = effective_challenge_store
    application.state.auth_store = effective_auth_store
    application.state.webauthn_backend = effective_webauthn_backend
    application.state.payment_provider = effective_payment_provider
    application.state.fulfillment_provider = effective_fulfillment_provider
    application.state.payment_webhook_queue = effective_payment_webhook_queue
    application.state.readiness_service = readiness_service or build_readiness_service(
        effective_settings
    )
    application.state.redis_client = redis_client
    application.state.operator_rate_limiter = operator_rate_limiter or (
        RedisOperatorRateLimiter(redis_client) if redis_client is not None else None
    )
    application.state.mcp_rate_limiter = mcp_rate_limiter or (
        RedisMcpRateLimiter(redis_client) if redis_client is not None else None
    )
    application.state.human_presence_store = human_presence_store or (
        RedisHumanPresenceStore(redis_client) if redis_client is not None else None
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=effective_settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Accept", "Authorization", "Content-Type", "X-CSRF-Token"],
    )
    register_domain_exception_handlers(application)
    application.include_router(health_router)
    application.include_router(api_v1_router)

    return application
