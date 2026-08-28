"""Thin merchant management routes."""

from fastapi import APIRouter, status

from app.api.v1.dependencies import MerchantApplicationDependency
from app.schemas.merchants import MerchantCreate, MerchantPatch, MerchantResponse

router = APIRouter(prefix="/merchants", tags=["merchants"])


@router.post("", response_model=MerchantResponse, status_code=status.HTTP_201_CREATED)
async def create_merchant(
    payload: MerchantCreate,
    application_service: MerchantApplicationDependency,
) -> MerchantResponse:
    merchant = await application_service.create(payload)
    return MerchantResponse.model_validate(merchant)


@router.get("", response_model=list[MerchantResponse])
async def list_merchants(
    application_service: MerchantApplicationDependency,
) -> list[MerchantResponse]:
    merchants = await application_service.list()
    return [MerchantResponse.model_validate(merchant) for merchant in merchants]


@router.get("/{merchant_id}", response_model=MerchantResponse)
async def get_merchant(
    merchant_id: str,
    application_service: MerchantApplicationDependency,
) -> MerchantResponse:
    merchant = await application_service.get(merchant_id)
    return MerchantResponse.model_validate(merchant)


@router.patch("/{merchant_id}", response_model=MerchantResponse)
async def update_merchant(
    merchant_id: str,
    payload: MerchantPatch,
    application_service: MerchantApplicationDependency,
) -> MerchantResponse:
    merchant = await application_service.update(merchant_id, payload)
    return MerchantResponse.model_validate(merchant)
