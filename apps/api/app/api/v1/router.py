"""Aggregate router for version 1 domain APIs."""

from fastapi import APIRouter

from app.api.v1.catalog import router as catalog_router
from app.api.v1.merchants import router as merchants_router
from app.api.v1.services import router as services_router

router = APIRouter(prefix="/api/v1")
router.include_router(merchants_router)
router.include_router(services_router)
router.include_router(catalog_router)
