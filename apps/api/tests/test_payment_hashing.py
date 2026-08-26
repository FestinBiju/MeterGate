from types import SimpleNamespace

from app.domain.enums import PaymentProvider, PurchaseType
from app.domain.payment_hashing import (
    PAYMENT_BINDING_VERSION,
    build_payment_binding_payload,
    calculate_payment_binding_hash,
    recompute_payment_binding_hash,
)

HASH_1 = f"sha256:{'1' * 64}"
HASH_2 = f"sha256:{'2' * 64}"
HASH_3 = f"sha256:{'3' * 64}"


def payment_fields() -> dict[str, object]:
    return {
        "transaction_id": "txn_00000000000000000000000001",
        "payment_binding_version": PAYMENT_BINDING_VERSION,
        "account_id": "acct_00000000000000000000000001",
        "authorization_id": "aut_00000000000000000000000001",
        "authorization_hash": HASH_1,
        "evaluation_id": "pye_00000000000000000000000001",
        "policy_id": "pol_00000000000000000000000001",
        "policy_hash": HASH_2,
        "quote_id": "qte_00000000000000000000000001",
        "quote_hash": HASH_3,
        "merchant_id": "mrc_00000000000000000000000001",
        "service_id": "svc_00000000000000000000000001",
        "amount": 500,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "provider": PaymentProvider.RAZORPAY,
        "provider_receipt": "txn_00000000000000000000000001",
    }


def test_payment_binding_is_deterministic_and_binds_authoritative_intent() -> None:
    fields = payment_fields()
    expected = calculate_payment_binding_hash(**fields)

    assert expected == calculate_payment_binding_hash(**dict(reversed(list(fields.items()))))
    assert expected.startswith("sha256:")
    assert len(expected) == 71
    for field, changed_value in (
        ("authorization_hash", f"sha256:{'4' * 64}"),
        ("account_id", "acct_00000000000000000000000002"),
        ("amount", 501),
        ("provider_receipt", "txn_00000000000000000000000002"),
    ):
        assert calculate_payment_binding_hash(**{**fields, field: changed_value}) != expected


def test_payment_binding_payload_is_versioned_nested_and_recomputable() -> None:
    fields = payment_fields()
    expected = calculate_payment_binding_hash(**fields)
    payload = build_payment_binding_payload(**fields)
    stored = SimpleNamespace(
        id=fields["transaction_id"],
        payment_binding_version=fields["payment_binding_version"],
        account_id=fields["account_id"],
        authorization_id=fields["authorization_id"],
        authorization_hash=fields["authorization_hash"],
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
        provider=fields["provider"],
        provider_receipt=fields["provider_receipt"],
        payment_binding_hash=expected,
    )

    assert payload["payment_binding_version"] == "1"
    assert payload["authorization"] == {
        "id": fields["authorization_id"],
        "hash": HASH_1,
    }
    assert payload["policy"] == {"id": fields["policy_id"], "hash": HASH_2}
    assert payload["quote"] == {"id": fields["quote_id"], "hash": HASH_3}
    assert payload["pricing"] == {
        "amount": 500,
        "currency": "INR",
        "purchase_type": "one_time",
    }
    assert payload["provider"] == "razorpay"
    assert "payment_binding_hash" not in payload
    assert recompute_payment_binding_hash(stored) == expected
