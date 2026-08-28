"""Application assembly for the public agent-readable catalog."""

from datetime import UTC, datetime

from app.domain.exceptions import ResourceNotFoundError
from app.models import Merchant, Service
from app.repositories.services import ServiceRepository
from app.schemas.catalog import (
    CatalogMerchant,
    CatalogMerchantReference,
    CatalogPricing,
    CatalogResponse,
    CatalogService,
    CatalogServiceDetail,
)


class CatalogApplicationService:
    def __init__(self, repository: ServiceRepository) -> None:
        self._repository = repository

    async def list(self) -> CatalogResponse:
        rows = await self._repository.list_active_catalog_rows()
        merchants: list[CatalogMerchant] = []
        merchant_indexes: dict[str, int] = {}

        for merchant, service in rows:
            merchant_index = merchant_indexes.get(merchant.id)
            if merchant_index is None:
                merchant_index = len(merchants)
                merchant_indexes[merchant.id] = merchant_index
                merchants.append(
                    CatalogMerchant(
                        **self._merchant_fields(merchant),
                        services=[],
                    )
                )
            merchants[merchant_index].services.append(self._catalog_service(service))

        return CatalogResponse(generated_at=datetime.now(UTC), merchants=merchants)

    async def get(self, service_id: str) -> CatalogServiceDetail:
        row = await self._repository.get_active_catalog_service(service_id)
        if row is None:
            raise ResourceNotFoundError("Catalog service", service_id)
        merchant, service = row
        return CatalogServiceDetail(
            generated_at=datetime.now(UTC),
            merchant=CatalogMerchantReference(**self._merchant_fields(merchant)),
            service=self._catalog_service(service),
        )

    @staticmethod
    def _merchant_fields(merchant: Merchant) -> dict[str, str]:
        return {
            "id": merchant.id,
            "slug": merchant.slug,
            "name": merchant.name,
            "description": merchant.description,
        }

    @staticmethod
    def _catalog_service(service: Service) -> CatalogService:
        return CatalogService(
            id=service.id,
            slug=service.slug,
            name=service.name,
            description=service.description,
            status=service.status,
            service_type=service.service_type,
            purchase_type=service.purchase_type,
            pricing=CatalogPricing(amount=service.base_price, currency=service.currency),
            input_schema=service.input_schema,
            output_schema=service.output_schema,
            output_content_type=service.output_content_type,
            maximum_fulfillment_seconds=service.maximum_fulfillment_seconds,
            refund_on_fulfillment_failure=service.refund_on_fulfillment_failure,
        )
