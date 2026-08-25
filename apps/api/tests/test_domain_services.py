from __future__ import annotations

from typing import Any

import pytest

from app.domain.enums import MerchantStatus, ServiceStatus, ServiceType
from app.domain.exceptions import (
    InvalidStateTransitionError,
    ResourceNotFoundError,
    SlugConflictError,
)
from app.models import Merchant, Service
from app.schemas.merchants import MerchantCreate, MerchantPatch
from app.schemas.services import ServiceCreate, ServicePatch
from app.services.catalog import CatalogApplicationService
from app.services.merchants import MerchantApplicationService
from app.services.services import ServiceApplicationService


class FakeMerchantRepository:
    def __init__(self, merchants: list[Merchant] | None = None) -> None:
        self.records = {merchant.id: merchant for merchant in merchants or []}

    async def create(self, merchant: Merchant) -> Merchant:
        self.records[merchant.id] = merchant
        return merchant

    async def get(self, merchant_id: str) -> Merchant | None:
        return self.records.get(merchant_id)

    async def get_by_slug(self, slug: str) -> Merchant | None:
        return next((item for item in self.records.values() if item.slug == slug), None)

    async def list(self) -> list[Merchant]:
        return sorted(self.records.values(), key=lambda item: (item.slug, item.id))

    async def update(self, merchant: Merchant) -> Merchant:
        self.records[merchant.id] = merchant
        return merchant


class FakeServiceRepository:
    def __init__(self, services: list[Service] | None = None) -> None:
        self.records = {service.id: service for service in services or []}
        self.catalog_rows: list[tuple[Merchant, Service]] = []

    async def create(self, service: Service) -> Service:
        self.records[service.id] = service
        return service

    async def get(self, service_id: str) -> Service | None:
        return self.records.get(service_id)

    async def get_by_slug(self, merchant_id: str, slug: str) -> Service | None:
        return next(
            (
                item
                for item in self.records.values()
                if item.merchant_id == merchant_id and item.slug == slug
            ),
            None,
        )

    async def list(self, merchant_id: str) -> list[Service]:
        return sorted(
            (item for item in self.records.values() if item.merchant_id == merchant_id),
            key=lambda item: (item.slug, item.id),
        )

    async def update(self, service: Service) -> Service:
        self.records[service.id] = service
        return service

    async def list_active_catalog_rows(self) -> list[tuple[Merchant, Service]]:
        return self.catalog_rows

    async def get_active_catalog_service(
        self,
        service_id: str,
    ) -> tuple[Merchant, Service] | None:
        return next((row for row in self.catalog_rows if row[1].id == service_id), None)


def make_merchant(
    merchant_id: str,
    slug: str,
    *,
    status: MerchantStatus = MerchantStatus.ACTIVE,
) -> Merchant:
    return Merchant(
        id=merchant_id,
        slug=slug,
        name=slug.title(),
        description=f"Synthetic description for {slug}.",
        status=status,
    )


def make_service(
    service_id: str,
    merchant_id: str,
    slug: str,
    *,
    status: ServiceStatus = ServiceStatus.ACTIVE,
    price: int = 500,
) -> Service:
    return Service(
        id=service_id,
        merchant_id=merchant_id,
        slug=slug,
        name=slug.replace("-", " ").title(),
        description=f"Synthetic description for {slug}.",
        status=status,
        service_type=ServiceType.REPORT,
        purchase_type="one_time",
        currency="INR",
        base_price=price,
        input_schema={"type": "object", "properties": {"norad_id": {"type": "integer"}}},
        output_schema={"type": "object"},
        output_content_type="application/json",
        maximum_fulfillment_seconds=30,
        refund_on_fulfillment_failure=True,
    )


def service_create_payload(**overrides: Any) -> ServiceCreate:
    values: dict[str, Any] = {
        "slug": "orbital-risk-report",
        "name": "Orbital Risk Report",
        "description": "Generate a current orbital-risk analysis.",
        "status": "active",
        "service_type": "report",
        "purchase_type": "one_time",
        "currency": "INR",
        "base_price": 500,
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "output_content_type": "application/json",
        "maximum_fulfillment_seconds": 30,
        "refund_on_fulfillment_failure": True,
    }
    values.update(overrides)
    return ServiceCreate.model_validate(values)


@pytest.mark.asyncio
async def test_merchant_service_creates_updates_and_rejects_duplicate_slug() -> None:
    repository = FakeMerchantRepository()
    application_service = MerchantApplicationService(repository)  # type: ignore[arg-type]
    payload = MerchantCreate(
        slug="orbitintel",
        name="OrbitIntel",
        description="Synthetic orbital intelligence.",
    )

    created = await application_service.create(payload)
    updated = await application_service.update(
        created.id,
        MerchantPatch(description="Updated synthetic description."),
    )

    assert created.id.startswith("mrc_")
    assert updated.description == "Updated synthetic description."
    with pytest.raises(SlugConflictError):
        await application_service.create(payload)
    with pytest.raises(ResourceNotFoundError):
        await application_service.get("mrc_missing")


@pytest.mark.asyncio
async def test_service_slug_uniqueness_is_scoped_to_merchant() -> None:
    first_merchant = make_merchant("mrc_first", "first")
    second_merchant = make_merchant("mrc_second", "second")
    merchant_repository = FakeMerchantRepository([first_merchant, second_merchant])
    repository = FakeServiceRepository()
    application_service = ServiceApplicationService(  # type: ignore[arg-type]
        merchant_repository,
        repository,
    )
    payload = service_create_payload()

    first = await application_service.create(first_merchant.id, payload)
    second = await application_service.create(second_merchant.id, payload)

    assert first.id.startswith("svc_")
    assert second.id.startswith("svc_")
    assert first.id != second.id
    with pytest.raises(SlugConflictError):
        await application_service.create(first_merchant.id, payload)
    with pytest.raises(ResourceNotFoundError):
        await application_service.create("mrc_missing", payload)


@pytest.mark.asyncio
async def test_service_status_transitions_are_explicit_and_archived_is_terminal() -> None:
    merchant = make_merchant("mrc_orbit", "orbitintel")
    active = make_service("svc_active", merchant.id, "active-service")
    archived = make_service(
        "svc_archived",
        merchant.id,
        "archived-service",
        status=ServiceStatus.ARCHIVED,
    )
    repository = FakeServiceRepository([active, archived])
    application_service = ServiceApplicationService(  # type: ignore[arg-type]
        FakeMerchantRepository([merchant]),
        repository,
    )

    updated = await application_service.update(
        active.id,
        ServicePatch(status=ServiceStatus.INACTIVE),
    )

    assert updated.status is ServiceStatus.INACTIVE
    with pytest.raises(InvalidStateTransitionError):
        await application_service.update(active.id, ServicePatch(status=ServiceStatus.DRAFT))
    with pytest.raises(InvalidStateTransitionError):
        await application_service.update(archived.id, ServicePatch(name="Cannot change"))


@pytest.mark.asyncio
async def test_catalog_uses_stable_machine_readable_shapes() -> None:
    merchant = make_merchant("mrc_orbit", "orbitintel")
    service = make_service("svc_risk", merchant.id, "orbital-risk", price=500)
    repository = FakeServiceRepository([service])
    repository.catalog_rows = [(merchant, service)]
    application_service = CatalogApplicationService(repository)  # type: ignore[arg-type]

    catalog = await application_service.list()
    detail = await application_service.get(service.id)

    assert catalog.version == "1"
    assert catalog.generated_at.tzinfo is not None
    assert len(catalog.merchants) == 1
    catalog_service = catalog.merchants[0].services[0]
    assert catalog_service.status == "active"
    assert catalog_service.pricing.model_dump() == {"amount": 500, "currency": "INR"}
    assert catalog_service.input_schema == service.input_schema
    assert catalog_service.output_schema == service.output_schema
    assert detail.service == catalog_service
    assert detail.merchant.description == merchant.description


@pytest.mark.asyncio
async def test_catalog_detail_hides_missing_or_inactive_rows() -> None:
    application_service = CatalogApplicationService(FakeServiceRepository())  # type: ignore[arg-type]

    with pytest.raises(ResourceNotFoundError):
        await application_service.get("svc_missing")
