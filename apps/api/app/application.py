"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_domain_exception_handlers
from app.api.health import router as health_router
from app.api.v1.router import router as api_v1_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import Database, create_database
from app.services.readiness import ReadinessService, build_readiness_service


def create_app(
    *,
    settings: Settings | None = None,
    readiness_service: ReadinessService | None = None,
    database: Database | None = None,
) -> FastAPI:
    """Build an application with injectable infrastructure readiness checks."""
    effective_settings = settings or get_settings()
    owns_database = database is None
    effective_database = database or create_database(effective_settings)
    configure_logging(effective_settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
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
