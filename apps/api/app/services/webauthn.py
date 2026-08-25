"""Project-owned WebAuthn boundary backed by the maintained py_webauthn library."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


class WebAuthnVerificationError(ValueError):
    """A deliberately sanitized WebAuthn verification failure."""

    def __init__(self) -> None:
        super().__init__("WebAuthn response verification failed")


@dataclass(frozen=True, slots=True)
class WebAuthnCredentialDescriptor:
    """A persisted credential reference safe to use in browser option generation."""

    credential_id: bytes
    transports: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RegistrationVerification:
    """Security-relevant output from a verified credential registration."""

    credential_id: bytes
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]
    device_type: str
    backed_up: bool
    user_verified: bool


@dataclass(frozen=True, slots=True)
class AuthenticationVerification:
    """Security-relevant output from a verified credential assertion."""

    credential_id: bytes
    new_sign_count: int
    device_type: str
    backed_up: bool
    user_verified: bool


@runtime_checkable
class WebAuthnBackend(Protocol):
    """Application-facing WebAuthn operations independent of a vendor library."""

    def registration_options(
        self,
        *,
        challenge: bytes,
        user_handle: bytes,
        user_name: str,
        user_display_name: str,
        exclude_credentials: Sequence[WebAuthnCredentialDescriptor] = (),
    ) -> dict[str, Any]: ...

    def verify_registration(
        self,
        *,
        credential: Mapping[str, Any],
        expected_challenge: bytes,
    ) -> RegistrationVerification: ...

    def authentication_options(
        self,
        *,
        challenge: bytes,
        allow_credentials: Sequence[WebAuthnCredentialDescriptor],
    ) -> dict[str, Any]: ...

    def verify_authentication(
        self,
        *,
        credential: Mapping[str, Any],
        expected_challenge: bytes,
        credential_public_key: bytes,
        credential_current_sign_count: int,
    ) -> AuthenticationVerification: ...


class PyWebAuthnBackend:
    """Real WebAuthn option generation and cryptographic response verification."""

    def __init__(
        self,
        *,
        rp_id: str,
        rp_name: str,
        expected_origins: Sequence[str],
        timeout_ms: int,
    ) -> None:
        if not rp_id or not rp_name or not expected_origins or timeout_ms <= 0:
            raise ValueError("WebAuthn backend configuration is incomplete")
        self._rp_id = rp_id
        self._rp_name = rp_name
        self._expected_origins = tuple(expected_origins)
        self._timeout_ms = timeout_ms

    def registration_options(
        self,
        *,
        challenge: bytes,
        user_handle: bytes,
        user_name: str,
        user_display_name: str,
        exclude_credentials: Sequence[WebAuthnCredentialDescriptor] = (),
    ) -> dict[str, Any]:
        self._validate_challenge(challenge)
        if not 1 <= len(user_handle) <= 64:
            raise ValueError("WebAuthn user handles must contain between 1 and 64 bytes")
        options = generate_registration_options(
            rp_id=self._rp_id,
            rp_name=self._rp_name,
            user_id=user_handle,
            user_name=user_name,
            user_display_name=user_display_name,
            challenge=challenge,
            timeout=self._timeout_ms,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=self._to_library_descriptors(exclude_credentials),
        )
        return self._browser_options(options_to_json(options))

    def verify_registration(
        self,
        *,
        credential: Mapping[str, Any],
        expected_challenge: bytes,
    ) -> RegistrationVerification:
        self._validate_challenge(expected_challenge)
        credential_value = dict(credential)
        try:
            verified = verify_registration_response(
                credential=credential_value,
                expected_challenge=expected_challenge,
                expected_rp_id=self._rp_id,
                expected_origin=list(self._expected_origins),
                require_user_presence=True,
                require_user_verification=True,
            )
        except (
            InvalidJSONStructure,
            InvalidRegistrationResponse,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise WebAuthnVerificationError() from error

        return RegistrationVerification(
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            transports=self._registration_transports(credential_value),
            device_type=verified.credential_device_type.value,
            backed_up=verified.credential_backed_up,
            user_verified=verified.user_verified,
        )

    def authentication_options(
        self,
        *,
        challenge: bytes,
        allow_credentials: Sequence[WebAuthnCredentialDescriptor],
    ) -> dict[str, Any]:
        self._validate_challenge(challenge)
        if not allow_credentials:
            raise ValueError("Approval authentication requires at least one credential")
        options = generate_authentication_options(
            rp_id=self._rp_id,
            challenge=challenge,
            timeout=self._timeout_ms,
            allow_credentials=self._to_library_descriptors(allow_credentials),
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return self._browser_options(options_to_json(options))

    def verify_authentication(
        self,
        *,
        credential: Mapping[str, Any],
        expected_challenge: bytes,
        credential_public_key: bytes,
        credential_current_sign_count: int,
    ) -> AuthenticationVerification:
        self._validate_challenge(expected_challenge)
        if credential_current_sign_count < 0:
            raise ValueError("Credential signature counters cannot be negative")
        try:
            verified = verify_authentication_response(
                credential=dict(credential),
                expected_challenge=expected_challenge,
                expected_rp_id=self._rp_id,
                expected_origin=list(self._expected_origins),
                credential_public_key=credential_public_key,
                credential_current_sign_count=credential_current_sign_count,
                require_user_verification=True,
            )
        except (
            InvalidAuthenticationResponse,
            InvalidJSONStructure,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise WebAuthnVerificationError() from error

        return AuthenticationVerification(
            credential_id=verified.credential_id,
            new_sign_count=verified.new_sign_count,
            device_type=verified.credential_device_type.value,
            backed_up=verified.credential_backed_up,
            user_verified=verified.user_verified,
        )

    @staticmethod
    def _validate_challenge(challenge: bytes) -> None:
        if not isinstance(challenge, bytes) or len(challenge) < 32:
            raise ValueError("WebAuthn challenges must contain at least 32 random bytes")

    @staticmethod
    def _to_library_descriptors(
        descriptors: Sequence[WebAuthnCredentialDescriptor],
    ) -> list[PublicKeyCredentialDescriptor]:
        return [
            PublicKeyCredentialDescriptor(
                id=descriptor.credential_id,
                transports=(
                    [AuthenticatorTransport(value) for value in descriptor.transports]
                    if descriptor.transports
                    else None
                ),
            )
            for descriptor in descriptors
        ]

    @staticmethod
    def _browser_options(serialized: str) -> dict[str, Any]:
        value = json.loads(serialized)
        if not isinstance(value, dict):
            raise ValueError("WebAuthn option serialization did not produce an object")
        return value

    @staticmethod
    def _registration_transports(credential: Mapping[str, Any]) -> tuple[str, ...]:
        response = credential.get("response")
        if not isinstance(response, Mapping):
            return ()
        transports = response.get("transports")
        if not isinstance(transports, list):
            return ()

        supported: list[str] = []
        for value in transports:
            if not isinstance(value, str):
                continue
            try:
                normalized = AuthenticatorTransport(value).value
            except ValueError:
                continue
            if normalized not in supported:
                supported.append(normalized)
        return tuple(supported)
