"""Application operations and lifecycle rules for merchant services."""

from sqlalchemy.exc import IntegrityError

from app.domain.enums import ServiceStatus
from app.domain.exceptions import (
    InvalidStateTransitionError,
    ResourceNotFoundError,
    SlugConflictError,
)
from app.domain.ids import new_service_id
from app.models import Service
from app.repositories.merchants import MerchantRepository
from app.repositories.services import ServiceRepository
from app.schemas.services import ServiceCreate, ServicePatch

_ALLOWED_STATUS_TRANSITIONS: dict[ServiceStatus, frozenset[ServiceStatus]] = {
    ServiceStatus.DRAFT: frozenset(
        {ServiceStatus.ACTIVE, ServiceStatus.INACTIVE, ServiceStatus.ARCHIVED}
    ),
    ServiceStatus.ACTIVE: frozenset({ServiceStatus.INACTIVE, ServiceStatus.ARCHIVED}),
    ServiceStatus.INACTIVE: frozenset({ServiceStatus.ACTIVE, ServiceStatus.ARCHIVED}),
    ServiceStatus.ARCHIVED: frozenset(),
}


class ServiceApplicationService:
    def __init__(
        self,
        merchant_repository: MerchantRepository,
        service_repository: ServiceRepository,
    ) -> None:
        self._merchant_repository = merchant_repository
        self._service_repository = service_repository

    async def create(self, merchant_id: str, payload: ServiceCreate) -> Service:
        await self._require_merchant(merchant_id)
        if await self._service_repository.get_by_slug(merchant_id, payload.slug) is not None:
            raise SlugConflictError("Service", payload.slug)

        service = Service(
            id=new_service_id(),
            merchant_id=merchant_id,
            **payload.model_dump(),
        )
        try:
            return await self._service_repository.create(service)
        except IntegrityError as error:
            raise SlugConflictError("Service", payload.slug) from error

    async def get(self, service_id: str) -> Service:
        service = await self._service_repository.get(service_id)
        if service is None:
            raise ResourceNotFoundError("Service", service_id)
        return service

    async def list(self, merchant_id: str) -> list[Service]:
        await self._require_merchant(merchant_id)
        return await self._service_repository.list(merchant_id)

    async def update(self, service_id: str, payload: ServicePatch) -> Service:
        service = await self.get(service_id)
        if service.status is ServiceStatus.ARCHIVED:
            raise InvalidStateTransitionError(
                f"Service '{service_id}' is archived and cannot be updated"
            )

        changes = payload.model_dump(exclude_unset=True)
        requested_status = changes.get("status")
        if requested_status is not None and requested_status is not service.status:
            self._validate_status_transition(service, requested_status)

        new_slug = changes.get("slug")
        if new_slug is not None and new_slug != service.slug:
            existing = await self._service_repository.get_by_slug(service.merchant_id, new_slug)
            if existing is not None and existing.id != service.id:
                raise SlugConflictError("Service", new_slug)

        for field_name, value in changes.items():
            setattr(service, field_name, value)

        try:
            return await self._service_repository.update(service)
        except IntegrityError as error:
            raise SlugConflictError("Service", str(new_slug or service.slug)) from error

    async def _require_merchant(self, merchant_id: str) -> None:
        if await self._merchant_repository.get(merchant_id) is None:
            raise ResourceNotFoundError("Merchant", merchant_id)

    @staticmethod
    def _validate_status_transition(service: Service, requested_status: ServiceStatus) -> None:
        if requested_status not in _ALLOWED_STATUS_TRANSITIONS[service.status]:
            raise InvalidStateTransitionError(
                f"Service '{service.id}' cannot transition from "
                f"'{service.status.value}' to '{requested_status.value}'"
            )
