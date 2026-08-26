import re

import pytest

from app.domain import ids

ID_PATTERN = re.compile(
    r"^(aid_|ach_|aut_|mrc_|pkc_|pmt_|pol_|pte_|pye_|qte_|rwe_|svc_|txn_)"
    r"[0-7][0-9A-HJKMNP-TV-Z]{25}$"
)
ACCOUNT_ID_PATTERN = re.compile(r"^acct_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def test_domain_ids_have_expected_prefix_length_and_alphabet() -> None:
    account_id = ids.new_account_id()
    standard_ids = (
        ids.new_approval_identity_id(),
        ids.new_approval_challenge_id(),
        ids.new_authorization_id(),
        ids.new_merchant_id(),
        ids.new_payment_attempt_id(),
        ids.new_passkey_credential_id(),
        ids.new_policy_id(),
        ids.new_payment_transaction_event_id(),
        ids.new_policy_evaluation_id(),
        ids.new_quote_id(),
        ids.new_razorpay_webhook_event_id(),
        ids.new_service_id(),
        ids.new_payment_transaction_id(),
    )

    assert len(account_id) == 31
    assert ACCOUNT_ID_PATTERN.fullmatch(account_id)
    assert all(len(identifier) == 30 for identifier in standard_ids)
    assert all(ID_PATTERN.fullmatch(identifier) for identifier in standard_ids)


def test_domain_ids_are_unique_and_monotonic_within_one_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ids.time, "time_ns", lambda: 1_700_000_000_000_000_000)
    monkeypatch.setattr(ids.secrets, "randbits", lambda bits: 42)
    monkeypatch.setattr(ids, "_last_timestamp_ms", -1)
    monkeypatch.setattr(ids, "_last_randomness", -1)

    generated = [ids.new_quote_id() for _ in range(100)]

    assert generated == sorted(generated)
    assert len(generated) == len(set(generated))


def test_domain_ids_reject_unknown_prefixes() -> None:
    with pytest.raises(ValueError, match="Unsupported MeterGate ID prefix"):
        ids.generate_id("bad_")  # type: ignore[arg-type]
