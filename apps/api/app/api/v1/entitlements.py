"""Buyer-owned entitlement lookup and capability issuance routes."""

from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from app.api.v1.dependencies import (
    AuthenticatedMutationDependency,
    CurrentAccountDependency,
    EntitlementApplicationDependency,
)
from app.schemas.entitlements import (
    CapabilityResponse,
    EntitlementLookupResponse,
    EntitlementResponse,
    ResourceParty,
)
from app.services.entitlements import EntitlementView

router = APIRouter(tags=["entitlements"])


def _response(view: EntitlementView) -> EntitlementResponse:
    entitlement = view.entitlement
    return EntitlementResponse(
        entitlement_id=entitlement.id,
        transaction_id=entitlement.transaction_id,
        merchant=ResourceParty(
            id=view.merchant.id,
            slug=view.merchant.slug,
            name=view.merchant.name,
        ),
        service=ResourceParty(
            id=view.service.id,
            slug=view.service.slug,
            name=view.service.name,
        ),
        input=entitlement.input,
        input_hash=entitlement.input_hash,
        amount=entitlement.amount,
        currency=entitlement.currency,
        purchase_type=entitlement.purchase_type,
        maximum_executions=1,
        issued_at=entitlement.issued_at,
        expires_at=entitlement.expires_at,
        state=(
            "active"
            if entitlement.expires_at.timestamp() > datetime.now(UTC).timestamp()
            else "expired"
        ),
        entitlement_version="1",
        entitlement_hash=entitlement.entitlement_hash,
    )


@router.get(
    "/payment-transactions/{transaction_id}/entitlement",
    response_model=EntitlementLookupResponse,
    responses={202: {"model": EntitlementLookupResponse}},
)
async def get_transaction_entitlement(
    transaction_id: str,
    response: Response,
    application_service: EntitlementApplicationDependency,
    current: CurrentAccountDependency,
) -> EntitlementLookupResponse:
    lookup = await application_service.lookup_for_transaction(
        transaction_id,
        account_id=current.account.id,
    )
    entitlement_response = None
    timeline = await application_service.timeline_for_transaction(
        transaction_id,
        account_id=current.account.id,
    )
    if lookup.entitlement is not None:
        view = await application_service.get_view(
            lookup.entitlement.id,
            account_id=current.account.id,
        )
        entitlement_response = _response(view)
    response.status_code = 202 if lookup.state == "pending" else 200
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return EntitlementLookupResponse(
        transaction_id=lookup.transaction_id,
        state=lookup.state,
        reason_code=lookup.reason_code,
        entitlement=entitlement_response,
        timeline=[
            {
                "event_type": entry.event_type,
                "reason_code": entry.reason_code,
                "occurred_at": entry.occurred_at,
            }
            for entry in timeline
        ],
    )


@router.get(
    "/entitlements/{entitlement_id}",
    response_model=EntitlementResponse,
)
async def get_entitlement(
    entitlement_id: str,
    response: Response,
    application_service: EntitlementApplicationDependency,
    current: CurrentAccountDependency,
) -> EntitlementResponse:
    view = await application_service.get_view(
        entitlement_id,
        account_id=current.account.id,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return _response(view)


@router.post(
    "/entitlements/{entitlement_id}/capability",
    response_model=CapabilityResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_entitlement_capability(
    entitlement_id: str,
    response: Response,
    application_service: EntitlementApplicationDependency,
    current: AuthenticatedMutationDependency,
) -> CapabilityResponse:
    issued = await application_service.issue_capability(
        entitlement_id,
        account_id=current.account.id,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return CapabilityResponse(
        entitlement_id=issued.claims.entitlement_id,
        token=issued.token,
        expires_at=issued.claims.expires_at,
        maximum_executions=1,
    )
