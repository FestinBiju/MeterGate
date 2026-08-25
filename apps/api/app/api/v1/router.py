"""Aggregate router for version 1 domain APIs."""

from fastapi import APIRouter

from app.api.v1.approval_identities import router as approval_identities_router
from app.api.v1.approvals import router as approvals_router
from app.api.v1.auth import router as auth_router
from app.api.v1.catalog import router as catalog_router
from app.api.v1.merchants import router as merchants_router
from app.api.v1.policies import router as policies_router
from app.api.v1.policy_evaluations import router as policy_evaluations_router
from app.api.v1.quotes import router as quotes_router
from app.api.v1.services import router as services_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(approval_identities_router)
router.include_router(approvals_router)
router.include_router(merchants_router)
router.include_router(services_router)
router.include_router(catalog_router)
router.include_router(quotes_router)
router.include_router(policies_router)
router.include_router(policy_evaluations_router)
