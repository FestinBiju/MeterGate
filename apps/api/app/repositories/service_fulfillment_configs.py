"""Revision-owned persistence for private service fulfillment routing."""

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ServiceFulfillmentConfig


class ServiceFulfillmentConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, config_id: str) -> ServiceFulfillmentConfig | None:
        return await self._session.get(ServiceFulfillmentConfig, config_id)

    async def get_by_service_id(self, service_id: str) -> ServiceFulfillmentConfig | None:
        return await self._session.scalar(
            select(ServiceFulfillmentConfig).where(
                ServiceFulfillmentConfig.service_id == service_id
            )
        )

    async def get_by_service_id_for_update(
        self,
        service_id: str,
    ) -> ServiceFulfillmentConfig | None:
        return await self._session.scalar(
            select(ServiceFulfillmentConfig)
            .where(ServiceFulfillmentConfig.service_id == service_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def create(self, config: ServiceFulfillmentConfig) -> ServiceFulfillmentConfig:
        if config.revision != 1:
            raise ValueError("A new service fulfillment config must start at revision 1")
        self._session.add(config)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(config)
        return config

    async def update(self, config: ServiceFulfillmentConfig) -> ServiceFulfillmentConfig:
        if sqlalchemy_inspect(config).session is not self._session.sync_session:
            raise ValueError("Service fulfillment config must be loaded on this database session")
        config.revision += 1
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(config)
        return config
