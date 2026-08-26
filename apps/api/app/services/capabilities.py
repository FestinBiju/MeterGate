"""Short-lived, exact-resource MeterGate bearer capabilities."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.domain.exceptions import (
    CapabilityExpiredError,
    CapabilityForbiddenError,
    CapabilityInvalidError,
)
from app.domain.ids import new_capability_id

CAPABILITY_ISSUER = "MeterGate"
CAPABILITY_AUDIENCE = "MeterGate protected resource gateway"
CAPABILITY_ALGORITHM = "HS256"
_CLAIM_NAMES = frozenset(
    {
        "iss",
        "aud",
        "jti",
        "account_id",
        "entitlement_id",
        "transaction_id",
        "merchant_id",
        "service_id",
        "quote_id",
        "quote_hash",
        "input_hash",
        "maximum_executions",
        "iat",
        "exp",
    }
)
_IDENTIFIER_PATTERNS = {
    "jti": re.compile(r"^cap_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
    "account_id": re.compile(r"^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
    "entitlement_id": re.compile(r"^ent_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
    "transaction_id": re.compile(r"^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
    "merchant_id": re.compile(r"^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
    "service_id": re.compile(r"^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
    "quote_id": re.compile(r"^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
}
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CapabilityClaims:
    jti: str
    account_id: str
    entitlement_id: str
    transaction_id: str
    merchant_id: str
    service_id: str
    quote_id: str
    quote_hash: str
    input_hash: str
    maximum_executions: int
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedCapability:
    token: str
    claims: CapabilityClaims


class CapabilityTokenService:
    """Issue and verify JWTs with a fixed algorithm, issuer, and audience."""

    def __init__(
        self,
        secret: str,
        *,
        ttl: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
            raise ValueError("Capability secret must contain at least 32 bytes")
        if not timedelta(seconds=30) <= ttl <= timedelta(minutes=10):
            raise ValueError("Capability lifetime must be between 30 and 600 seconds")
        self._secret = secret
        self._ttl = ttl
        self._clock = clock

    def issue(self, entitlement: Any) -> IssuedCapability:
        now = self._read_clock()
        entitlement_expiry = self._as_utc(entitlement.expires_at)
        expires_at = min(now + self._ttl, entitlement_expiry)
        if expires_at <= now:
            raise CapabilityExpiredError(
                "The entitlement has expired",
                "ENTITLEMENT_EXPIRED",
            )
        claims = CapabilityClaims(
            jti=new_capability_id(),
            account_id=entitlement.account_id,
            entitlement_id=entitlement.id,
            transaction_id=entitlement.transaction_id,
            merchant_id=entitlement.merchant_id,
            service_id=entitlement.service_id,
            quote_id=entitlement.quote_id,
            quote_hash=entitlement.quote_hash,
            input_hash=entitlement.input_hash,
            maximum_executions=entitlement.maximum_executions,
            issued_at=now,
            expires_at=expires_at,
        )
        payload = self._payload(claims)
        token = jwt.encode(payload, self._secret, algorithm=CAPABILITY_ALGORITHM)
        return IssuedCapability(token=token, claims=claims)

    def verify(self, token: str) -> CapabilityClaims:
        if not isinstance(token, str) or not token or len(token) > 4_096 or token != token.strip():
            raise CapabilityInvalidError(
                "The capability is invalid",
                "CAPABILITY_INVALID",
            )
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[CAPABILITY_ALGORITHM],
                audience=CAPABILITY_AUDIENCE,
                issuer=CAPABILITY_ISSUER,
                options={
                    "require": sorted(_CLAIM_NAMES),
                    "verify_signature": True,
                    # PyJWT intentionally uses wall-clock time internally. The
                    # service validates these claims below against its injected
                    # UTC clock so tests and production share one deterministic
                    # source of time.
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError as error:
            raise CapabilityExpiredError(
                "The capability has expired",
                "CAPABILITY_EXPIRED",
            ) from error
        except jwt.InvalidAudienceError as error:
            raise CapabilityForbiddenError(
                "The capability audience is invalid",
                "CAPABILITY_AUDIENCE_MISMATCH",
            ) from error
        except jwt.PyJWTError as error:
            raise CapabilityInvalidError(
                "The capability is invalid",
                "CAPABILITY_INVALID",
            ) from error
        return self._validated_claims(payload)

    @staticmethod
    def require_binding(
        claims: CapabilityClaims,
        *,
        entitlement_id: str,
        transaction_id: str,
        merchant_id: str,
        service_id: str,
        quote_id: str,
        quote_hash: str,
        input_hash: str,
    ) -> None:
        expected = (
            entitlement_id,
            transaction_id,
            merchant_id,
            service_id,
            quote_id,
            quote_hash,
            input_hash,
        )
        actual = (
            claims.entitlement_id,
            claims.transaction_id,
            claims.merchant_id,
            claims.service_id,
            claims.quote_id,
            claims.quote_hash,
            claims.input_hash,
        )
        if claims.maximum_executions != 1 or actual != expected:
            raise CapabilityForbiddenError(
                "The capability does not bind this exact resource and input",
                "CAPABILITY_RESOURCE_MISMATCH",
            )

    @staticmethod
    def _payload(claims: CapabilityClaims) -> dict[str, object]:
        return {
            "iss": CAPABILITY_ISSUER,
            "aud": CAPABILITY_AUDIENCE,
            "jti": claims.jti,
            "account_id": claims.account_id,
            "entitlement_id": claims.entitlement_id,
            "transaction_id": claims.transaction_id,
            "merchant_id": claims.merchant_id,
            "service_id": claims.service_id,
            "quote_id": claims.quote_id,
            "quote_hash": claims.quote_hash,
            "input_hash": claims.input_hash,
            "maximum_executions": claims.maximum_executions,
            "iat": int(claims.issued_at.timestamp()),
            "exp": int(claims.expires_at.timestamp()),
        }

    def _validated_claims(self, payload: Mapping[str, object]) -> CapabilityClaims:
        if set(payload) != _CLAIM_NAMES:
            raise CapabilityInvalidError(
                "The capability claim set is invalid",
                "CAPABILITY_INVALID",
            )
        if payload.get("iss") != CAPABILITY_ISSUER or payload.get("aud") != (CAPABILITY_AUDIENCE):
            raise CapabilityInvalidError(
                "The capability authority binding is invalid",
                "CAPABILITY_INVALID",
            )
        for name, pattern in _IDENTIFIER_PATTERNS.items():
            value = payload.get(name)
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise CapabilityInvalidError(
                    "The capability contains an invalid identifier",
                    "CAPABILITY_INVALID",
                )
        for name in ("quote_hash", "input_hash"):
            value = payload.get(name)
            if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
                raise CapabilityInvalidError(
                    "The capability contains an invalid integrity hash",
                    "CAPABILITY_INVALID",
                )
        maximum_executions = payload.get("maximum_executions")
        if type(maximum_executions) is not int or maximum_executions != 1:
            raise CapabilityInvalidError(
                "The capability execution bound is invalid",
                "CAPABILITY_INVALID",
            )
        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        if type(issued_at) is not int or type(expires_at) is not int or expires_at <= issued_at:
            raise CapabilityInvalidError(
                "The capability lifetime is invalid",
                "CAPABILITY_INVALID",
            )
        try:
            issued_datetime = datetime.fromtimestamp(issued_at, tz=UTC)
            expiry_datetime = datetime.fromtimestamp(expires_at, tz=UTC)
        except (OSError, OverflowError, ValueError) as error:
            raise CapabilityInvalidError(
                "The capability lifetime is invalid",
                "CAPABILITY_INVALID",
            ) from error
        now = self._read_clock()
        if issued_datetime > now:
            raise CapabilityInvalidError(
                "The capability issue time is invalid",
                "CAPABILITY_INVALID",
            )
        if expiry_datetime <= now:
            raise CapabilityExpiredError(
                "The capability has expired",
                "CAPABILITY_EXPIRED",
            )
        return CapabilityClaims(
            jti=str(payload["jti"]),
            account_id=str(payload["account_id"]),
            entitlement_id=str(payload["entitlement_id"]),
            transaction_id=str(payload["transaction_id"]),
            merchant_id=str(payload["merchant_id"]),
            service_id=str(payload["service_id"]),
            quote_id=str(payload["quote_id"]),
            quote_hash=str(payload["quote_hash"]),
            input_hash=str(payload["input_hash"]),
            maximum_executions=1,
            issued_at=issued_datetime,
            expires_at=expiry_datetime,
        )

    def _read_clock(self) -> datetime:
        return self._as_utc(self._clock()).replace(microsecond=0)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Capability timestamps must be timezone-aware")
        return value.astimezone(UTC)
