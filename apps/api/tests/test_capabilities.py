"""Capability cryptography and exact-binding regression tests."""

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
    CapabilityTokenService,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
SECRET = "test-capability-secret-with-more-than-32-bytes"


def _entitlement() -> SimpleNamespace:
    return SimpleNamespace(
        id="ent_01M00000000000000000000000",
        account_id="acct_01M00000000000000000000000",
        transaction_id="txn_01M00000000000000000000000",
        merchant_id="mrc_01M00000000000000000000000",
        service_id="svc_01M00000000000000000000000",
        quote_id="qte_01M00000000000000000000000",
        quote_hash="sha256:" + "1" * 64,
        input_hash="sha256:" + "2" * 64,
        maximum_executions=1,
        expires_at=NOW + timedelta(minutes=10),
    )


def _service(*, now: datetime = NOW) -> CapabilityTokenService:
    return CapabilityTokenService(
        SECRET,
        ttl=timedelta(minutes=5),
        clock=lambda: now,
    )


def test_capability_round_trip_and_exact_binding() -> None:
    entitlement = _entitlement()
    issued = _service().issue(entitlement)

    claims = _service(now=NOW + timedelta(seconds=1)).verify(issued.token)

    assert claims.entitlement_id == entitlement.id
    assert claims.maximum_executions == 1
    CapabilityTokenService.require_binding(
        claims,
        entitlement_id=entitlement.id,
        transaction_id=entitlement.transaction_id,
        merchant_id=entitlement.merchant_id,
        service_id=entitlement.service_id,
        quote_id=entitlement.quote_id,
        quote_hash=entitlement.quote_hash,
        input_hash=entitlement.input_hash,
    )


def test_tampered_capability_is_rejected() -> None:
    token = _service().issue(_entitlement()).token
    header, payload, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    tampered = ".".join((header, payload, replacement + signature[1:]))
    with pytest.raises(CapabilityInvalidError) as caught:
        _service().verify(tampered)
    assert caught.value.reason_code == "CAPABILITY_INVALID"


def test_expired_capability_is_rejected() -> None:
    issued = _service().issue(_entitlement())
    with pytest.raises(CapabilityExpiredError) as caught:
        _service(now=NOW + timedelta(minutes=6)).verify(issued.token)
    assert caught.value.reason_code == "CAPABILITY_EXPIRED"


def test_wrong_audience_is_rejected_with_stable_code() -> None:
    issued = _service().issue(_entitlement())
    payload = jwt.decode(
        issued.token,
        SECRET,
        algorithms=[CAPABILITY_ALGORITHM],
        options={"verify_aud": False, "verify_iat": False, "verify_exp": False},
    )
    payload["aud"] = "another gateway"
    token = jwt.encode(payload, SECRET, algorithm=CAPABILITY_ALGORITHM)

    with pytest.raises(CapabilityForbiddenError) as caught:
        _service().verify(token)
    assert caught.value.reason_code == "CAPABILITY_AUDIENCE_MISMATCH"


@pytest.mark.parametrize(
    "field,value",
    [
        ("entitlement_id", "ent_01M00000000000000000000001"),
        ("transaction_id", "txn_01M00000000000000000000001"),
        ("merchant_id", "mrc_01M00000000000000000000001"),
        ("service_id", "svc_01M00000000000000000000001"),
        ("quote_id", "qte_01M00000000000000000000001"),
        ("quote_hash", "sha256:" + "3" * 64),
        ("input_hash", "sha256:" + "4" * 64),
    ],
)
def test_wrong_capability_binding_is_rejected(field: str, value: str) -> None:
    entitlement = _entitlement()
    claims = _service().verify(_service().issue(entitlement).token)
    expected = {
        "entitlement_id": entitlement.id,
        "transaction_id": entitlement.transaction_id,
        "merchant_id": entitlement.merchant_id,
        "service_id": entitlement.service_id,
        "quote_id": entitlement.quote_id,
        "quote_hash": entitlement.quote_hash,
        "input_hash": entitlement.input_hash,
    }
    expected[field] = value

    with pytest.raises(CapabilityForbiddenError) as caught:
        CapabilityTokenService.require_binding(claims, **expected)
    assert caught.value.reason_code == "CAPABILITY_RESOURCE_MISMATCH"
