"""Passkey-first signup, login, opaque session, and credential-metadata routes."""

from fastapi import APIRouter, Depends, Response

from app.api.v1.dependencies import (
    AllowedOriginDependency,
    AuthenticatedMutationDependency,
    AuthenticationApplicationDependency,
    CurrentAccountDependency,
    SettingsDependency,
    disable_private_caching,
)
from app.schemas.auth import (
    AuthCeremonyOptionsResponse,
    AuthCeremonyVerify,
    AuthLogoutResponse,
    AuthPasskeyListResponse,
    AuthSessionResponse,
    SignupOptionsCreate,
)
from app.services.auth import IssuedAuthSession

router = APIRouter(
    prefix="/auth",
    tags=["authentication"],
    dependencies=[Depends(disable_private_caching)],
)


def _set_session_cookie(
    response: Response,
    issued: IssuedAuthSession,
    settings: SettingsDependency,
) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=issued.session_id,
        max_age=settings.auth_session_ttl_seconds,
        expires=issued.response.expires_at,
        path=settings.auth_cookie_path,
        domain=settings.auth_cookie_domain,
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite=settings.auth_cookie_samesite,
    )


def _delete_session_cookie(response: Response, settings: SettingsDependency) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path=settings.auth_cookie_path,
        domain=settings.auth_cookie_domain,
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite=settings.auth_cookie_samesite,
    )


@router.post("/signup/options", response_model=AuthCeremonyOptionsResponse)
async def create_signup_options(
    payload: SignupOptionsCreate,
    application_service: AuthenticationApplicationDependency,
    origin: AllowedOriginDependency,
) -> AuthCeremonyOptionsResponse:
    del origin
    return await application_service.signup_options(payload)


@router.post("/signup/verify", response_model=AuthSessionResponse)
async def verify_signup(
    payload: AuthCeremonyVerify,
    response: Response,
    settings: SettingsDependency,
    application_service: AuthenticationApplicationDependency,
    origin: AllowedOriginDependency,
) -> AuthSessionResponse:
    del origin
    issued = await application_service.signup_verify(payload)
    _set_session_cookie(response, issued, settings)
    return issued.response


@router.post("/login/options", response_model=AuthCeremonyOptionsResponse)
async def create_login_options(
    application_service: AuthenticationApplicationDependency,
    origin: AllowedOriginDependency,
) -> AuthCeremonyOptionsResponse:
    del origin
    return await application_service.login_options()


@router.post("/login/verify", response_model=AuthSessionResponse)
async def verify_login(
    payload: AuthCeremonyVerify,
    response: Response,
    settings: SettingsDependency,
    application_service: AuthenticationApplicationDependency,
    origin: AllowedOriginDependency,
) -> AuthSessionResponse:
    del origin
    issued = await application_service.login_verify(payload)
    _set_session_cookie(response, issued, settings)
    return issued.response


@router.post("/reauth/options", response_model=AuthCeremonyOptionsResponse)
async def create_reauthentication_options(
    current: AuthenticatedMutationDependency,
    application_service: AuthenticationApplicationDependency,
) -> AuthCeremonyOptionsResponse:
    del current
    return await application_service.login_options()


@router.post("/reauth/verify", response_model=AuthSessionResponse)
async def verify_reauthentication(
    payload: AuthCeremonyVerify,
    response: Response,
    settings: SettingsDependency,
    current: AuthenticatedMutationDependency,
    application_service: AuthenticationApplicationDependency,
) -> AuthSessionResponse:
    issued = await application_service.reauthenticate(current, payload)
    _set_session_cookie(response, issued, settings)
    return issued.response


@router.get("/session", response_model=AuthSessionResponse)
async def get_auth_session(current: CurrentAccountDependency) -> AuthSessionResponse:
    return current.response


@router.post("/logout", response_model=AuthLogoutResponse)
async def logout(
    response: Response,
    settings: SettingsDependency,
    current: AuthenticatedMutationDependency,
    application_service: AuthenticationApplicationDependency,
) -> AuthLogoutResponse:
    result = await application_service.logout(current.state.session_id)
    _delete_session_cookie(response, settings)
    return result


@router.get("/passkeys", response_model=AuthPasskeyListResponse)
async def list_passkeys(
    current: CurrentAccountDependency,
    application_service: AuthenticationApplicationDependency,
) -> AuthPasskeyListResponse:
    return await application_service.list_passkeys(current)
