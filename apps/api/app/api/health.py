"""Liveness and infrastructure-readiness routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.schemas.health import HealthResponse, ReadinessResponse
from app.services.readiness import ReadinessService

router = APIRouter(tags=["health"])


def get_readiness_service(request: Request) -> ReadinessService:
    """Resolve the application-scoped readiness service."""
    return request.app.state.readiness_service


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Report process liveness without depending on external services."""
    return HealthResponse(status="ok", service=request.app.state.settings.service_name)


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(
    request: Request,
    response: Response,
    readiness_service: Annotated[ReadinessService, Depends(get_readiness_service)],
) -> ReadinessResponse:
    """Report whether PostgreSQL and Redis are reachable."""
    report = await readiness_service.check(service_name=request.app.state.settings.service_name)
    if not report.is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report
