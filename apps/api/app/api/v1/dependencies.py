"""Request-scoped application-service dependencies."""

from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.session import get_session
from app.repositories.merchants import MerchantRepository
from app.repositories.quotes import QuoteRepository
from app.repositories.services import ServiceRepository
from app.services.catalog import CatalogApplicationService
from app.services.merchants import MerchantApplicationService
from app.services.quotes import QuoteApplicationService
from app.services.services import ServiceApplicationService

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def get_application_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError("Application settings are not configured")
    return settings


SettingsDependency = Annotated[Settings, Depends(get_application_settings)]


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
