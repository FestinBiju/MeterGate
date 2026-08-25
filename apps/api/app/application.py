"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.api.errors import register_domain_exception_handlers
from app.api.health import router as health_router
from app.api.v1.router import router as api_v1_router
from app.cache.approval_challenges import ChallengeStore, RedisChallengeStore
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import Database, create_database
from app.services.readiness import ReadinessService, build_readiness_service
from app.services.webauthn import PyWebAuthnBackend, WebAuthnBackend


def create_app(
    *,
    settings: Settings | None = None,
    readiness_service: ReadinessService | None = None,
    database: Database | None = None,
    challenge_store: ChallengeStore | None = None,
    webauthn_backend: WebAuthnBackend | None = None,
) -> FastAPI:
    """Build an application with injectable durable and ephemeral infrastructure."""
    effective_settings = settings or get_settings()
    owns_database = database is None
    effective_database = database or create_database(effective_settings)
    redis_client: Redis | None = None
    if challenge_store is None:
        redis_client = Redis.from_url(
            effective_settings.redis_url.get_secret_value(),
            decode_responses=False,
        )
        effective_challenge_store: ChallengeStore = RedisChallengeStore(redis_client)
    else:
        effective_challenge_store = challenge_store
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
    application.state.webauthn_backend = effective_webauthn_backend
    application.state.readiness_service = readiness_service or build_readiness_service(
        effective_settings
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=effective_settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Accept", "Content-Type"],
    )
    register_domain_exception_handlers(application)
    application.include_router(health_router)
    application.include_router(api_v1_router)

    return application
