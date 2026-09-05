"""Thin service management routes."""

from fastapi import APIRouter, Depends, status

from app.api.v1.dependencies import ServiceApplicationDependency, require_catalog_admin
from app.schemas.services import ServiceCreate, ServicePatch, ServiceResponse

router = APIRouter(tags=["services"])


@router.post(
    "/merchants/{merchant_id}/services",
    response_model=ServiceResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_catalog_admin)],
)
async def create_service(
    merchant_id: str,
    payload: ServiceCreate,
    application_service: ServiceApplicationDependency,
) -> ServiceResponse:
    service = await application_service.create(merchant_id, payload)
    return ServiceResponse.model_validate(service)


@router.get(
    "/merchants/{merchant_id}/services",
    response_model=list[ServiceResponse],
)
async def list_services(
    merchant_id: str,
    application_service: ServiceApplicationDependency,
) -> list[ServiceResponse]:
    services = await application_service.list(merchant_id)
    return [ServiceResponse.model_validate(service) for service in services]


@router.get("/services/{service_id}", response_model=ServiceResponse)
async def get_service(
    service_id: str,
    application_service: ServiceApplicationDependency,
) -> ServiceResponse:
    service = await application_service.get(service_id)
    return ServiceResponse.model_validate(service)


@router.patch(
    "/services/{service_id}",
    response_model=ServiceResponse,
    dependencies=[Depends(require_catalog_admin)],
)
async def update_service(
    service_id: str,
    payload: ServicePatch,
    application_service: ServiceApplicationDependency,
) -> ServiceResponse:
    service = await application_service.update(service_id, payload)
    return ServiceResponse.model_validate(service)
