"""Thin HTTP routes for immutable server-issued quotes."""

from fastapi import APIRouter, status

from app.api.v1.dependencies import QuoteApplicationDependency
from app.schemas.quotes import QuoteCreate, QuoteResponse

router = APIRouter(tags=["quotes"])


@router.post(
    "/quotes",
    response_model=QuoteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_quote(
    payload: QuoteCreate,
    application_service: QuoteApplicationDependency,
) -> QuoteResponse:
    return await application_service.create(payload)


@router.get("/quotes/{quote_id}", response_model=QuoteResponse)
async def get_quote(
    quote_id: str,
    application_service: QuoteApplicationDependency,
) -> QuoteResponse:
    return await application_service.get(quote_id)
