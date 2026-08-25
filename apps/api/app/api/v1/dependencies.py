"""Request-scoped application-service dependencies."""

from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.approval_challenges import ChallengeStore
from app.core.config import Settings
from app.db.session import get_session
from app.repositories.approval_identities import ApprovalIdentityRepository
from app.repositories.authorizations import PurchaseAuthorizationRepository
from app.repositories.buyer_policies import BuyerPolicyRepository
from app.repositories.merchants import MerchantRepository
from app.repositories.passkey_credentials import PasskeyCredentialRepository
from app.repositories.policy_evaluations import PolicyEvaluationRepository
from app.repositories.quotes import QuoteRepository
from app.repositories.services import ServiceRepository
from app.services.approvals import ApprovalApplicationService
from app.services.catalog import CatalogApplicationService
from app.services.merchants import MerchantApplicationService
from app.services.passkeys import PasskeyApplicationService
from app.services.policies import BuyerPolicyApplicationService
from app.services.policy_evaluations import PolicyEvaluationApplicationService
from app.services.quotes import QuoteApplicationService
from app.services.services import ServiceApplicationService
from app.services.webauthn import WebAuthnBackend

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def get_application_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError("Application settings are not configured")
    return settings


SettingsDependency = Annotated[Settings, Depends(get_application_settings)]


def get_challenge_store(request: Request) -> ChallengeStore:
    challenge_store = getattr(request.app.state, "challenge_store", None)
    if not isinstance(challenge_store, ChallengeStore):
        raise RuntimeError("Approval challenge storage is not configured")
    return challenge_store


def get_webauthn_backend(request: Request) -> WebAuthnBackend:
    webauthn_backend = getattr(request.app.state, "webauthn_backend", None)
    if not isinstance(webauthn_backend, WebAuthnBackend):
        raise RuntimeError("WebAuthn backend is not configured")
    return webauthn_backend


ChallengeStoreDependency = Annotated[ChallengeStore, Depends(get_challenge_store)]
WebAuthnBackendDependency = Annotated[WebAuthnBackend, Depends(get_webauthn_backend)]


def get_merchant_application_service(
    session: SessionDependency,
) -> MerchantApplicationService:
    return MerchantApplicationService(MerchantRepository(session))


def get_service_application_service(
    session: SessionDependency,
) -> ServiceApplicationService:
    return ServiceApplicationService(
        MerchantRepository(session),
        ServiceRepository(session),
    )


def get_catalog_application_service(
    session: SessionDependency,
) -> CatalogApplicationService:
    return CatalogApplicationService(ServiceRepository(session))


def get_quote_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
) -> QuoteApplicationService:
    return QuoteApplicationService(
        ServiceRepository(session),
        QuoteRepository(session),
        ttl=timedelta(seconds=settings.quote_ttl_seconds),
    )


def get_buyer_policy_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
) -> BuyerPolicyApplicationService:
    return BuyerPolicyApplicationService(
        BuyerPolicyRepository(session),
        maximum_ttl=timedelta(seconds=settings.policy_max_ttl_seconds),
    )


def get_policy_evaluation_application_service(
    session: SessionDependency,
) -> PolicyEvaluationApplicationService:
    return PolicyEvaluationApplicationService(
        BuyerPolicyRepository(session),
        QuoteRepository(session),
        PolicyEvaluationRepository(session),
    )


def get_passkey_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
    challenge_store: ChallengeStoreDependency,
    webauthn_backend: WebAuthnBackendDependency,
) -> PasskeyApplicationService:
    return PasskeyApplicationService(
        ApprovalIdentityRepository(session),
        PasskeyCredentialRepository(session),
        challenge_store,
        webauthn_backend,
        challenge_ttl=timedelta(seconds=settings.webauthn_challenge_ttl_seconds),
    )


def get_approval_application_service(
    session: SessionDependency,
    settings: SettingsDependency,
    challenge_store: ChallengeStoreDependency,
    webauthn_backend: WebAuthnBackendDependency,
) -> ApprovalApplicationService:
    return ApprovalApplicationService(
        ApprovalIdentityRepository(session),
        PasskeyCredentialRepository(session),
        PurchaseAuthorizationRepository(session),
        BuyerPolicyRepository(session),
        QuoteRepository(session),
        PolicyEvaluationRepository(session),
        challenge_store,
        webauthn_backend,
        challenge_ttl=timedelta(seconds=settings.webauthn_challenge_ttl_seconds),
        authorization_ttl=timedelta(seconds=settings.authorization_ttl_seconds),
    )


MerchantApplicationDependency = Annotated[
    MerchantApplicationService,
    Depends(get_merchant_application_service),
]
ServiceApplicationDependency = Annotated[
    ServiceApplicationService,
    Depends(get_service_application_service),
]
CatalogApplicationDependency = Annotated[
    CatalogApplicationService,
    Depends(get_catalog_application_service),
]
QuoteApplicationDependency = Annotated[
    QuoteApplicationService,
    Depends(get_quote_application_service),
]
BuyerPolicyApplicationDependency = Annotated[
    BuyerPolicyApplicationService,
    Depends(get_buyer_policy_application_service),
]
PolicyEvaluationApplicationDependency = Annotated[
    PolicyEvaluationApplicationService,
    Depends(get_policy_evaluation_application_service),
]
PasskeyApplicationDependency = Annotated[
    PasskeyApplicationService,
    Depends(get_passkey_application_service),
]
ApprovalApplicationDependency = Annotated[
    ApprovalApplicationService,
    Depends(get_approval_application_service),
]
