"""Application operations for merchant management."""

from sqlalchemy.exc import IntegrityError

from app.domain.exceptions import ResourceNotFoundError, SlugConflictError
from app.domain.ids import new_merchant_id
from app.models import Merchant
from app.repositories.merchants import MerchantRepository
from app.schemas.merchants import MerchantCreate, MerchantPatch


class MerchantApplicationService:
    def __init__(self, repository: MerchantRepository) -> None:
        self._repository = repository

    async def create(self, payload: MerchantCreate) -> Merchant:
        if await self._repository.get_by_slug(payload.slug) is not None:
            raise SlugConflictError("Merchant", payload.slug)

        merchant = Merchant(id=new_merchant_id(), **payload.model_dump())
        try:
            return await self._repository.create(merchant)
        except IntegrityError as error:
            raise SlugConflictError("Merchant", payload.slug) from error

    async def get(self, merchant_id: str) -> Merchant:
        merchant = await self._repository.get(merchant_id)
        if merchant is None:
            raise ResourceNotFoundError("Merchant", merchant_id)
        return merchant

    async def list(self) -> list[Merchant]:
        return await self._repository.list()

    async def update(self, merchant_id: str, payload: MerchantPatch) -> Merchant:
        merchant = await self.get(merchant_id)
        changes = payload.model_dump(exclude_unset=True)
        new_slug = changes.get("slug")
        if new_slug is not None and new_slug != merchant.slug:
            existing = await self._repository.get_by_slug(new_slug)
            if existing is not None and existing.id != merchant.id:
                raise SlugConflictError("Merchant", new_slug)

        for field_name, value in changes.items():
            setattr(merchant, field_name, value)

        try:
            return await self._repository.update(merchant)
        except IntegrityError as error:
            raise SlugConflictError("Merchant", str(new_slug or merchant.slug)) from error
