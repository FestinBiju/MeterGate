"""Defense-in-depth tests for short-lived exact-resource JWT capabilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest

from app.domain.exceptions import (
    CapabilityExpiredError,
    CapabilityForbiddenError,
    CapabilityInvalidError,
)
from app.services.capabilities import (
    CAPABILITY_ALGORITHM,
    CAPABILITY_AUDIENCE,
    CAPABILITY_ISSUER,
    CapabilityTokenService,
)

NOW = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)
SECRET = "capability-test-secret-that-is-at-least-32-bytes"
OTHER_SECRET = "different-test-secret-that-is-at-least-32-bytes"


def _entitlement(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": "ent_01M00000000000000000000000",
        "account_id": "acct_01M00000000000000000000000",
        "transaction_id": "txn_01M00000000000000000000000",
        "merchant_id": "mrc_01M00000000000000000000000",
        "service_id": "svc_01M00000000000000000000000",
        "quote_id": "qte_01M00000000000000000000000",
        "quote_hash": "sha256:" + "1" * 64,
        "input_hash": "sha256:" + "2" * 64,
        "maximum_executions": 1,
        "expires_at": NOW + timedelta(minutes=10),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _service(*, now: datetime = NOW, ttl_seconds: int = 300) -> CapabilityTokenService:
    return CapabilityTokenService(
        SECRET,
        ttl=timedelta(seconds=ttl_seconds),
        clock=lambda: now,
    )


def _unverified_payload(token: str) -> dict[str, object]:
    return jwt.decode(token, options={"verify_signature": False})


def _signed(payload: dict[str, object], *, secret: str = SECRET) -> str:
    return jwt.encode(payload, secret, algorithm=CAPABILITY_ALGORITHM)


def test_issued_capability_contains_only_the_explicit_safe_claim_set() -> None:
    issued = _service().issue(_entitlement())

    payload = _unverified_payload(issued.token)
    assert set(payload) == {
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
    assert payload["iss"] == CAPABILITY_ISSUER
    assert payload["aud"] == CAPABILITY_AUDIENCE
    assert payload["maximum_executions"] == 1
    assert not {
        "razorpay_key_secret",
        "webhook_secret",
        "session_id",
        "csrf_token",
        "passkey",
        "payment_instrument",
    }.intersection(payload)


def test_entitlement_expiry_caps_token_lifetime() -> None:
    entitlement = _entitlement(expires_at=NOW + timedelta(seconds=45))

    issued = _service(ttl_seconds=300).issue(entitlement)
    verified = _service(now=NOW + timedelta(seconds=44)).verify(issued.token)

    assert issued.claims.expires_at == NOW + timedelta(seconds=45)
    assert verified.expires_at == NOW + timedelta(seconds=45)


def test_expired_entitlement_cannot_produce_a_capability() -> None:
    with pytest.raises(CapabilityExpiredError) as caught:
        _service().issue(_entitlement(expires_at=NOW))

    assert caught.value.reason_code == "ENTITLEMENT_EXPIRED"


@pytest.mark.parametrize(
    ("secret", "ttl_seconds"),
    [
        ("too-short", 300),
        (SECRET, 29),
        (SECRET, 601),
    ],
)
def test_weak_secret_and_non_short_lifetimes_are_rejected(
    secret: str,
    ttl_seconds: int,
) -> None:
    with pytest.raises(ValueError):
        CapabilityTokenService(
            secret,
            ttl=timedelta(seconds=ttl_seconds),
            clock=lambda: NOW,
        )


def test_verification_uses_the_injected_clock_for_expiry() -> None:
    token = _service(ttl_seconds=30).issue(_entitlement()).token

    with pytest.raises(CapabilityExpiredError) as caught:
        _service(now=NOW + timedelta(seconds=30)).verify(token)

    assert caught.value.reason_code == "CAPABILITY_EXPIRED"


def test_future_issue_time_is_rejected_against_the_injected_clock() -> None:
    token = _service().issue(_entitlement()).token

    with pytest.raises(CapabilityInvalidError) as caught:
        _service(now=NOW - timedelta(seconds=1)).verify(token)

    assert caught.value.reason_code == "CAPABILITY_INVALID"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda payload: payload.pop("input_hash"), id="missing-claim"),
        pytest.param(lambda payload: payload.update({"scope": "admin"}), id="extra-claim"),
        pytest.param(
            lambda payload: payload.update({"maximum_executions": 2}),
            id="execution-bound",
        ),
        pytest.param(
            lambda payload: payload.update({"maximum_executions": True}),
            id="boolean-execution-bound",
        ),
        pytest.param(
            lambda payload: payload.update({"maximum_executions": 1.0}),
            id="float-execution-bound",
        ),
        pytest.param(
            lambda payload: payload.update({"entitlement_id": "ent_invalid"}),
            id="identifier",
        ),
        pytest.param(
            lambda payload: payload.update({"input_hash": "sha256:" + "A" * 64}),
            id="hash",
        ),
        pytest.param(
            lambda payload: payload.update({"exp": payload["iat"]}),
            id="empty-lifetime",
        ),
        pytest.param(
            lambda payload: payload.update({"iat": str(payload["iat"])}),
            id="numeric-date-type",
        ),
    ],
)
def test_signed_but_structurally_invalid_claims_are_rejected(mutate: object) -> None:
    payload = _unverified_payload(_service().issue(_entitlement()).token)
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(CapabilityInvalidError) as caught:
        _service().verify(_signed(payload))

    assert caught.value.reason_code == "CAPABILITY_INVALID"


def test_wrong_signing_key_and_algorithm_are_rejected() -> None:
    payload = _unverified_payload(_service().issue(_entitlement()).token)
    wrong_key = _signed(payload, secret=OTHER_SECRET)
    wrong_algorithm = jwt.encode(payload, SECRET, algorithm="HS384")

    for token in (wrong_key, wrong_algorithm):
        with pytest.raises(CapabilityInvalidError) as caught:
            _service().verify(token)
        assert caught.value.reason_code == "CAPABILITY_INVALID"


def test_tampering_a_signature_byte_is_rejected() -> None:
    token = _service().issue(_entitlement()).token
    header, payload, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    tampered = ".".join((header, payload, replacement + signature[1:]))

    with pytest.raises(CapabilityInvalidError) as caught:
        _service().verify(tampered)

    assert caught.value.reason_code == "CAPABILITY_INVALID"


def test_wrong_audience_is_distinguished_from_a_malformed_token() -> None:
    payload = _unverified_payload(_service().issue(_entitlement()).token)
    payload["aud"] = "untrusted resource gateway"

    with pytest.raises(CapabilityForbiddenError) as caught:
        _service().verify(_signed(payload))

    assert caught.value.reason_code == "CAPABILITY_AUDIENCE_MISMATCH"


def test_audience_array_is_rejected_even_when_it_contains_the_expected_value() -> None:
    payload = _unverified_payload(_service().issue(_entitlement()).token)
    payload["aud"] = [CAPABILITY_AUDIENCE]

    with pytest.raises(CapabilityInvalidError) as caught:
        _service().verify(_signed(payload))

    assert caught.value.reason_code == "CAPABILITY_INVALID"


@pytest.mark.parametrize("token", ["", " token", "token ", "x" * 4_097])
def test_malformed_or_ambiguously_delimited_token_is_rejected(token: str) -> None:
    with pytest.raises(CapabilityInvalidError) as caught:
        _service().verify(token)

    assert caught.value.reason_code == "CAPABILITY_INVALID"
