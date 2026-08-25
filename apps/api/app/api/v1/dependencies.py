"""Request-scoped application-service dependencies."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.repositories.merchants import MerchantRepository
from app.repositories.services import ServiceRepository
from app.services.catalog import CatalogApplicationService
from app.services.merchants import MerchantApplicationService
from app.services.services import ServiceApplicationService

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


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
