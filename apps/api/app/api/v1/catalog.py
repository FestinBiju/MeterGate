"""Public machine-readable catalog routes."""

from fastapi import APIRouter

from app.api.v1.dependencies import CatalogApplicationDependency
from app.schemas.catalog import CatalogResponse, CatalogServiceDetail

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("", response_model=CatalogResponse)
async def list_catalog(
    application_service: CatalogApplicationDependency,
) -> CatalogResponse:
    return await application_service.list()


@router.get("/services/{service_id}", response_model=CatalogServiceDetail)
async def get_catalog_service(
    service_id: str,
    application_service: CatalogApplicationDependency,
) -> CatalogServiceDetail:
    return await application_service.get(service_id)
