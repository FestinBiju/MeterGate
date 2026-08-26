from datetime import UTC, datetime, timedelta

from app.domain.entitlement_hashing import (
    ENTITLEMENT_HASH_PAYLOAD_VERSION,
    build_entitlement_hash_payload,
    calculate_entitlement_hash,
    recompute_entitlement_integrity,
)
from app.domain.enums import PurchaseType
from app.domain.hashing import sha256_json

HASH = "sha256:" + "1" * 64


def entitlement_fields() -> dict[str, object]:
    issued_at = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    return {
        "entitlement_id": "ent_00000000000000000000000000",
        "account_id": "acct_00000000000000000000000000",
        "transaction_id": "txn_00000000000000000000000000",
        "payment_binding_hash": HASH,
        "payment_reverification_event_id": "pte_00000000000000000000000000",
        "payment_reverification_revision": 7,
        "provider_order_id": "order_exact",
        "provider_payment_id": "pay_exact",
        "authorization_id": "aut_00000000000000000000000000",
        "authorization_hash": HASH,
        "evaluation_id": "pye_00000000000000000000000000",
        "policy_id": "pol_00000000000000000000000000",
        "policy_hash": HASH,
        "quote_id": "qte_00000000000000000000000000",
        "quote_hash": HASH,
        "merchant_id": "mrc_00000000000000000000000000",
        "service_id": "svc_00000000000000000000000000",
        "input_value": {"norad_id": 25544},
        "input_hash": sha256_json({"norad_id": 25544}),
        "amount": 900,
        "currency": "INR",
        "purchase_type": PurchaseType.ONE_TIME,
        "maximum_executions": 1,
        "issued_at": issued_at,
        "expires_at": issued_at + timedelta(minutes=10),
        "entitlement_version": ENTITLEMENT_HASH_PAYLOAD_VERSION,
    }


def test_entitlement_hash_is_deterministic_and_binds_provider_reverification() -> None:
    fields = entitlement_fields()
    digest = calculate_entitlement_hash(**fields)

    assert digest == calculate_entitlement_hash(**fields)
    assert digest.startswith("sha256:")
    assert len(digest) == 71

    for field, changed in (
        ("payment_reverification_event_id", "pte_00000000000000000000000001"),
        ("payment_reverification_revision", 8),
        ("provider_order_id", "order_changed"),
        ("provider_payment_id", "pay_changed"),
    ):
        altered = fields | {field: changed}
        assert calculate_entitlement_hash(**altered) != digest


def test_entitlement_payload_uses_stable_version_and_exact_input() -> None:
    payload = build_entitlement_hash_payload(**entitlement_fields())

    assert payload["version"] == "1"
    assert payload["input"] == {"norad_id": 25544}
    assert payload["transaction"] == {
        "id": "txn_00000000000000000000000000",
        "payment_binding_hash": HASH,
        "reverification_event_id": "pte_00000000000000000000000000",
        "reverification_revision": 7,
        "provider_order_id": "order_exact",
        "provider_payment_id": "pay_exact",
    }


def test_recompute_entitlement_integrity_uses_only_persisted_fields() -> None:
    fields = entitlement_fields()

    class PersistedEntitlement:
        pass

    record = PersistedEntitlement()
    aliases = {
        "entitlement_id": "id",
        "input_value": "input",
    }
    for name, value in fields.items():
        setattr(record, aliases.get(name, name), value)
    record.entitlement_hash = calculate_entitlement_hash(**fields)

    integrity = recompute_entitlement_integrity(record)

    assert integrity.input_hash == fields["input_hash"]
    assert integrity.entitlement_hash == record.entitlement_hash
