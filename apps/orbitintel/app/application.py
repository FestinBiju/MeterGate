"""FastAPI application factory for the private OrbitIntel process."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime

import httpx2
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from redis.asyncio import Redis

from app.api.internal import router as internal_router
from app.cache import RedisClient, RedisGPSnapshotCache
from app.celestrak import CelesTrakClient, GPProvider
from app.core.config import Settings, get_settings
from app.errors import (
    CacheUnavailableError,
    CelesTrakNotFoundError,
    CelesTrakResponseError,
    CelesTrakUnavailableError,
    FaultInjectedError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyIntegrityError,
    IdempotencyUncertainError,
    InputBindingError,
    InternalAuthenticationError,
    OrbitIntelError,
    RequestBodyError,
    UnsupportedServiceError,
)
from app.idempotency import RedisExecutionIdempotency
from app.services import OrbitIntelService


def create_app(
    *,
    settings: Settings | None = None,
    redis_client: RedisClient | None = None,
    gp_provider: GPProvider | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    effective_settings = settings or get_settings()
    owns_redis = redis_client is None
    effective_redis: RedisClient = redis_client or Redis.from_url(
        effective_settings.redis_url.get_secret_value(),
        decode_responses=False,
        socket_connect_timeout=(effective_settings.orbitintel_redis_connect_timeout_seconds),
        socket_timeout=effective_settings.orbitintel_redis_socket_timeout_seconds,
        health_check_interval=(effective_settings.orbitintel_redis_health_check_interval_seconds),
        retry_on_timeout=False,
    )
    owns_http_client = gp_provider is None
    http_client: httpx2.AsyncClient | None = None
    if gp_provider is None:
        http_client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(
                connect=effective_settings.celestrak_connect_timeout_seconds,
                read=effective_settings.celestrak_read_timeout_seconds,
                write=effective_settings.celestrak_read_timeout_seconds,
                pool=effective_settings.celestrak_connect_timeout_seconds,
            ),
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": effective_settings.celestrak_user_agent,
            },
            limits=httpx2.Limits(
                max_connections=8,
                max_keepalive_connections=4,
                keepalive_expiry=30,
            ),
        )
        effective_provider: GPProvider = CelesTrakClient(
            http_client,
            maximum_response_bytes=effective_settings.celestrak_max_response_bytes,
            maximum_attempts=effective_settings.celestrak_max_attempts,
            retry_base_delay_seconds=(effective_settings.celestrak_retry_base_delay_seconds),
            request_timeout_seconds=(
                effective_settings.celestrak_connect_timeout_seconds
                + effective_settings.celestrak_read_timeout_seconds
            ),
            clock=clock,
        )
    else:
        effective_provider = gp_provider

    gp_cache = RedisGPSnapshotCache(
        effective_redis,
        ttl_seconds=effective_settings.orbitintel_gp_cache_ttl_seconds,
        lock_ttl_ms=effective_settings.orbitintel_gp_lock_ttl_ms,
        wait_seconds=effective_settings.orbitintel_gp_singleflight_wait_seconds,
        poll_seconds=effective_settings.orbitintel_gp_singleflight_poll_seconds,
    )
    idempotency = RedisExecutionIdempotency(
        effective_redis,
        ttl_seconds=effective_settings.orbitintel_idempotency_ttl_seconds,
        lock_ttl_seconds=effective_settings.orbitintel_idempotency_lock_ttl_seconds,
        wait_seconds=effective_settings.orbitintel_idempotency_wait_seconds,
        poll_seconds=effective_settings.orbitintel_idempotency_poll_seconds,
        maximum_result_bytes=effective_settings.orbitintel_result_max_bytes,
    )
    orbitintel_service = OrbitIntelService(
        effective_provider,
        gp_cache,
        clock=clock,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_http_client and http_client is not None:
                await http_client.aclose()
            if owns_redis:
                close = getattr(effective_redis, "aclose", None)
                if callable(close):
                    await close()

    docs_enabled = effective_settings.orbitintel_environment != "production"
    application = FastAPI(
        title="OrbitIntel Private Merchant",
        version="0.1.0",
        description="Private authenticated fulfillment service for MeterGate.",
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )
    application.state.settings = effective_settings
    application.state.orbitintel_service = orbitintel_service
    application.state.idempotency = idempotency

    @application.middleware("http")
    async def private_response_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.get("/health/live", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        return {"status": "ok", "service": "orbitintel"}

    _register_error_handlers(application)
    application.include_router(internal_router)
    return application


def _register_error_handlers(application: FastAPI) -> None:
    async def validation_handler(
        request: Request,
        error: Exception,
    ) -> JSONResponse:
        del request
        assert isinstance(error, RequestValidationError)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": "OrbitIntel request validation failed",
                "reason_code": "ORBITINTEL_REQUEST_INVALID",
            },
            headers={"Cache-Control": "private, no-store"},
        )

    application.add_exception_handler(RequestValidationError, validation_handler)
    mappings: tuple[tuple[type[OrbitIntelError], int], ...] = (
        (InternalAuthenticationError, status.HTTP_401_UNAUTHORIZED),
        (RequestBodyError, status.HTTP_422_UNPROCESSABLE_CONTENT),
        (InputBindingError, status.HTTP_422_UNPROCESSABLE_CONTENT),
        (UnsupportedServiceError, status.HTTP_422_UNPROCESSABLE_CONTENT),
        (CelesTrakNotFoundError, status.HTTP_404_NOT_FOUND),
        (CelesTrakUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE),
        (CelesTrakResponseError, status.HTTP_502_BAD_GATEWAY),
        (CacheUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE),
        (IdempotencyConflictError, status.HTTP_409_CONFLICT),
        (IdempotencyInProgressError, status.HTTP_409_CONFLICT),
        (IdempotencyUncertainError, status.HTTP_409_CONFLICT),
        (IdempotencyIntegrityError, status.HTTP_503_SERVICE_UNAVAILABLE),
    )
    for error_type, status_code in mappings:

        async def handler(
            request: Request,
            error: Exception,
            *,
            mapped_status: int = status_code,
        ) -> JSONResponse:
            del request
            assert isinstance(error, OrbitIntelError)
            effective_status = (
                status.HTTP_413_CONTENT_TOO_LARGE
                if isinstance(error, RequestBodyError)
                and error.reason_code == "ORBITINTEL_REQUEST_BODY_TOO_LARGE"
                else mapped_status
            )
            headers = {"Cache-Control": "private, no-store"}
            if isinstance(error, InternalAuthenticationError):
                headers["WWW-Authenticate"] = "Bearer"
            if isinstance(
                error,
                (
                    CelesTrakUnavailableError,
                    CacheUnavailableError,
                    IdempotencyInProgressError,
                    IdempotencyIntegrityError,
                ),
            ):
                headers["Retry-After"] = "1"
            content: dict[str, str] = {
                "detail": str(error),
                "reason_code": error.reason_code,
            }
            return JSONResponse(
                status_code=effective_status,
                content=content,
                headers=headers,
            )

        application.add_exception_handler(error_type, handler)

    async def fault_handler(request: Request, error: Exception) -> JSONResponse:
        del request
        assert isinstance(error, FaultInjectedError)
        status_code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if error.retryable
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        headers = {"Cache-Control": "private, no-store"}
        if error.retryable:
            headers["Retry-After"] = "1"
        return JSONResponse(
            status_code=status_code,
            content={"detail": str(error), "reason_code": error.reason_code},
            headers=headers,
        )

    application.add_exception_handler(FaultInjectedError, fault_handler)
