"""Async persistence operations for merchant services and catalog discovery."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MerchantStatus, ServiceStatus
from app.models import Merchant, Service


class ServiceRepository:
    """Keep service queries and persistence out of HTTP handlers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, service: Service) -> Service:
        self._session.add(service)
        await self._commit_and_refresh(service)
        return service

    async def get(self, service_id: str) -> Service | None:
        return await self._session.get(Service, service_id)

    async def get_by_slug(self, merchant_id: str, slug: str) -> Service | None:
        result = await self._session.scalars(
            select(Service).where(
                Service.merchant_id == merchant_id,
                Service.slug == slug,
            )
        )
        return result.one_or_none()

    async def list(self, merchant_id: str) -> list[Service]:
        result = await self._session.scalars(
            select(Service)
            .where(Service.merchant_id == merchant_id)
            .order_by(Service.slug.asc(), Service.id.asc())
        )
        return list(result.all())

    async def update(self, service: Service) -> Service:
        await self._commit_and_refresh(service)
        return service

    async def list_active_catalog_rows(self) -> list[tuple[Merchant, Service]]:
        result = await self._session.execute(
            select(Merchant, Service)
            .join(Service, Service.merchant_id == Merchant.id)
            .where(
                Merchant.status == MerchantStatus.ACTIVE,
                Service.status == ServiceStatus.ACTIVE,
            )
            .order_by(
                Merchant.slug.asc(),
                Merchant.id.asc(),
                Service.slug.asc(),
                Service.id.asc(),
            )
        )
        return [(merchant, service) for merchant, service in result.all()]

    async def get_active_catalog_service(
        self,
        service_id: str,
    ) -> tuple[Merchant, Service] | None:
        result = await self._session.execute(
            select(Merchant, Service)
            .join(Service, Service.merchant_id == Merchant.id)
            .where(
                Service.id == service_id,
                Merchant.status == MerchantStatus.ACTIVE,
                Service.status == ServiceStatus.ACTIVE,
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        merchant, service = row
        return merchant, service

    async def _commit_and_refresh(self, service: Service) -> None:
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(service)
