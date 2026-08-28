from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain.approval_hashing import (
    AUTHORIZATION_VERSION,
    build_approval_review_payload,
    build_authorization_hash_payload,
    calculate_approval_review_hash,
    calculate_authorization_hash,
    recompute_authorization_hash,
)
from app.domain.base64url import decode_base64url, encode_base64url
from app.domain.enums import PurchaseType
from app.services.webauthn import (
    PyWebAuthnBackend,
    WebAuthnCredentialDescriptor,
    WebAuthnVerificationError,
)

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
HASH_1 = f"sha256:{'1' * 64}"
HASH_2 = f"sha256:{'2' * 64}"
HASH_3 = f"sha256:{'3' * 64}"
HASH_4 = f"sha256:{'4' * 64}"


def review_fields() -> dict[str, object]:
    return {
        "evaluation_id": "pye_00000000000000000000000001",
        "evaluation_version": "1",
        "policy_id": "pol_00000000000000000000000001",
        "policy_hash": HASH_1,
        "quote_id": "qte_00000000000000000000000001",
        "quote_hash": HASH_2,
        "subject_ref": "dev-user-001",
        "approval_identity_id": "aid_00000000000000000000000001",
        "merchant_id": "mrc_00000000000000000000000001",
        "merchant_name": "OrbitIntel",
        "service_id": "svc_00000000000000000000000001",
        "service_name": "Orbital Risk Report",
        "amount": 500,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "quote_expires_at": NOW + timedelta(minutes=5),
        "policy_expires_at": NOW + timedelta(minutes=15),
        "policy_checks": [
            {
                "rule": "MAXIMUM_AMOUNT",
                "result": "pass",
                "reason_code": "PASS_AMOUNT_WITHIN_LIMIT",
                "details": {"maximum": 1000, "actual": 500},
                "explanation": "500 is within 1000 INR minor units",
            }
        ],
    }


def authorization_fields() -> dict[str, object]:
    return {
        "authorization_id": "aut_00000000000000000000000001",
        "authorization_version": AUTHORIZATION_VERSION,
        "approval_identity_id": "aid_00000000000000000000000001",
        "passkey_credential_id": "pkc_00000000000000000000000001",
        "subject_ref": "dev-user-001",
        "evaluation_id": "pye_00000000000000000000000001",
        "policy_id": "pol_00000000000000000000000001",
        "policy_hash": HASH_1,
        "quote_id": "qte_00000000000000000000000001",
        "quote_hash": HASH_2,
        "merchant_id": "mrc_00000000000000000000000001",
        "service_id": "svc_00000000000000000000000001",
        "amount": 500,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "review_hash": HASH_3,
        "challenge_hash": HASH_4,
        "authorized_at": NOW,
        "expires_at": NOW + timedelta(seconds=120),
    }


def test_base64url_round_trip_is_unpadded_and_canonical() -> None:
    encoded = encode_base64url(b"\xfb\xff\x00trusted-approval")

    assert encoded == "-_8AdHJ1c3RlZC1hcHByb3ZhbA"
    assert "=" not in encoded
    assert decode_base64url(encoded) == b"\xfb\xff\x00trusted-approval"


@pytest.mark.parametrize(
    "value",
    ["", "YQ==", "YQ=", "YQ+", "YQ/", "YQ!", "abcde", "\N{SNOWMAN}"],
)
def test_base64url_rejects_empty_padded_non_url_or_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid base64url"):
        decode_base64url(value)


def test_base64url_enforces_decoded_size_limit() -> None:
    with pytest.raises(ValueError, match="Invalid base64url"):
        decode_base64url(encode_base64url(b"x" * 5), maximum_bytes=4)


def test_review_hash_is_rfc8785_deterministic_and_binds_authoritative_fields() -> None:
    fields = review_fields()
    reordered = dict(reversed(list(fields.items())))

    assert calculate_approval_review_hash(**fields) == calculate_approval_review_hash(**reordered)
    original = calculate_approval_review_hash(**fields)
    changed = calculate_approval_review_hash(**{**fields, "amount": 501})

    assert original.startswith("sha256:")
    assert len(original) == 71
    assert changed != original
    review = build_approval_review_payload(**fields)
    assert review["approval_identity_id"] == fields["approval_identity_id"]
    assert review["merchant"] == {
        "id": fields["merchant_id"],
        "name": fields["merchant_name"],
    }
    assert review["quote_expires_at"] == "2026-08-25T12:05:00.000000Z"


def test_authorization_hash_is_deterministic_recomputable_and_binds_credential() -> None:
    fields = authorization_fields()
    expected = calculate_authorization_hash(**fields)
    stored = SimpleNamespace(
        id=fields["authorization_id"],
        approval_identity_id=fields["approval_identity_id"],
        passkey_credential_id=fields["passkey_credential_id"],
        subject_ref=fields["subject_ref"],
        evaluation_id=fields["evaluation_id"],
        policy_id=fields["policy_id"],
        policy_hash=fields["policy_hash"],
        quote_id=fields["quote_id"],
        quote_hash=fields["quote_hash"],
        merchant_id=fields["merchant_id"],
        service_id=fields["service_id"],
        amount=fields["amount"],
        currency=fields["currency"],
        purchase_type=fields["purchase_type"],
        review_hash=fields["review_hash"],
        challenge_hash=fields["challenge_hash"],
        authorized_at=fields["authorized_at"],
        expires_at=fields["expires_at"],
        authorization_version=fields["authorization_version"],
        authorization_hash=expected,
    )

    assert recompute_authorization_hash(stored) == expected
    assert (
        calculate_authorization_hash(
            **{**fields, "passkey_credential_id": "pkc_00000000000000000000000002"}
        )
        != expected
    )
    payload = build_authorization_hash_payload(**fields)
    assert payload["policy"] == {"id": fields["policy_id"], "hash": HASH_1}
    assert payload["quote"] == {"id": fields["quote_id"], "hash": HASH_2}
    assert "authorization_hash" not in payload


def backend() -> PyWebAuthnBackend:
    return PyWebAuthnBackend(
        rp_id="localhost",
        rp_name="MeterGate",
        expected_origins=["http://localhost:3000"],
        timeout_ms=300_000,
    )


def test_real_webauthn_registration_options_require_residency_and_uv() -> None:
    options = backend().registration_options(
        challenge=b"c" * 32,
        user_handle=b"u" * 32,
        user_name="dev-user-001",
        user_display_name="Development User",
        exclude_credentials=[WebAuthnCredentialDescriptor(b"existing", ("internal", "hybrid"))],
    )

    assert options["rp"] == {"id": "localhost", "name": "MeterGate"}
    assert options["challenge"] == encode_base64url(b"c" * 32)
    assert options["user"]["id"] == encode_base64url(b"u" * 32)
    assert options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["attestation"] == "none"
    assert options["excludeCredentials"][0]["id"] == encode_base64url(b"existing")


def test_real_webauthn_authentication_options_bind_allowlist_and_uv() -> None:
    options = backend().authentication_options(
        challenge=b"a" * 32,
        allow_credentials=[WebAuthnCredentialDescriptor(b"credential", ("internal",))],
    )

    assert options["rpId"] == "localhost"
    assert options["challenge"] == encode_base64url(b"a" * 32)
    assert options["userVerification"] == "required"
    assert options["allowCredentials"] == [
        {
            "id": encode_base64url(b"credential"),
            "type": "public-key",
            "transports": ["internal"],
        }
    ]


@pytest.mark.parametrize("challenge", [b"", b"short", bytearray(b"x" * 32)])
def test_real_webauthn_rejects_weak_or_wrong_type_challenges(challenge: object) -> None:
    with pytest.raises(ValueError, match="at least 32"):
        backend().authentication_options(
            challenge=challenge,  # type: ignore[arg-type]
            allow_credentials=[WebAuthnCredentialDescriptor(b"credential")],
        )


def test_real_webauthn_rejects_malformed_registration_and_assertion() -> None:
    with pytest.raises(WebAuthnVerificationError):
        backend().verify_registration(
            credential={"type": "public-key", "response": {}},
            expected_challenge=b"r" * 32,
        )
    with pytest.raises(WebAuthnVerificationError):
        backend().verify_authentication(
            credential={"type": "public-key", "response": {}},
            expected_challenge=b"a" * 32,
            credential_public_key=b"not-a-cose-key",
            credential_current_sign_count=0,
        )


def test_real_webauthn_configuration_and_counter_fail_closed() -> None:
    with pytest.raises(ValueError, match="configuration"):
        PyWebAuthnBackend(
            rp_id="",
            rp_name="MeterGate",
            expected_origins=["http://localhost:3000"],
            timeout_ms=300_000,
        )
    with pytest.raises(ValueError, match="cannot be negative"):
        backend().verify_authentication(
            credential={},
            expected_challenge=b"a" * 32,
            credential_public_key=b"key",
            credential_current_sign_count=-1,
        )
