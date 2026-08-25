import re

import pytest

from app.domain import ids

ID_PATTERN = re.compile(r"^(mrc_|pol_|pye_|qte_|svc_)[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def test_domain_ids_have_expected_prefix_length_and_alphabet() -> None:
    merchant_id = ids.new_merchant_id()
    policy_id = ids.new_policy_id()
    policy_evaluation_id = ids.new_policy_evaluation_id()
    quote_id = ids.new_quote_id()
    service_id = ids.new_service_id()

    assert len(merchant_id) == 30
    assert len(policy_id) == 30
    assert len(policy_evaluation_id) == 30
    assert len(quote_id) == 30
    assert len(service_id) == 30
    assert ID_PATTERN.fullmatch(merchant_id)
    assert ID_PATTERN.fullmatch(policy_id)
    assert ID_PATTERN.fullmatch(policy_evaluation_id)
    assert ID_PATTERN.fullmatch(quote_id)
    assert ID_PATTERN.fullmatch(service_id)


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
