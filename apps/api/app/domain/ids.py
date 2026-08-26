"""Crockford Base32 identifiers with a sortable ULID-compatible payload."""

import secrets
import threading
import time
from typing import Literal

IdPrefix = Literal[
    "acct_",
    "aid_",
    "ach_",
    "aut_",
    "mrc_",
    "pmt_",
    "pkc_",
    "pol_",
    "pte_",
    "pye_",
    "qte_",
    "rwe_",
    "svc_",
    "txn_",
]

ACCOUNT_ID_PREFIX: IdPrefix = "acct_"
APPROVAL_IDENTITY_ID_PREFIX: IdPrefix = "aid_"
APPROVAL_CHALLENGE_ID_PREFIX: IdPrefix = "ach_"
AUTHORIZATION_ID_PREFIX: IdPrefix = "aut_"
MERCHANT_ID_PREFIX: IdPrefix = "mrc_"
PAYMENT_ATTEMPT_ID_PREFIX: IdPrefix = "pmt_"
PASSKEY_CREDENTIAL_ID_PREFIX: IdPrefix = "pkc_"
POLICY_ID_PREFIX: IdPrefix = "pol_"
PAYMENT_TRANSACTION_EVENT_ID_PREFIX: IdPrefix = "pte_"
POLICY_EVALUATION_ID_PREFIX: IdPrefix = "pye_"
QUOTE_ID_PREFIX: IdPrefix = "qte_"
RAZORPAY_WEBHOOK_EVENT_ID_PREFIX: IdPrefix = "rwe_"
SERVICE_ID_PREFIX: IdPrefix = "svc_"
PAYMENT_TRANSACTION_ID_PREFIX: IdPrefix = "txn_"

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODED_LENGTH = 26
_MAX_TIMESTAMP = (1 << 48) - 1
_MAX_RANDOMNESS = (1 << 80) - 1
_generator_lock = threading.Lock()
_last_timestamp_ms = -1
_last_randomness = -1


def generate_id(prefix: IdPrefix) -> str:
    """Generate a prefixed, monotonic 48-bit time + 80-bit random identifier."""
    if prefix not in {
        ACCOUNT_ID_PREFIX,
        APPROVAL_IDENTITY_ID_PREFIX,
        APPROVAL_CHALLENGE_ID_PREFIX,
        AUTHORIZATION_ID_PREFIX,
        MERCHANT_ID_PREFIX,
        PAYMENT_ATTEMPT_ID_PREFIX,
        PASSKEY_CREDENTIAL_ID_PREFIX,
        POLICY_ID_PREFIX,
        PAYMENT_TRANSACTION_EVENT_ID_PREFIX,
        POLICY_EVALUATION_ID_PREFIX,
        QUOTE_ID_PREFIX,
        RAZORPAY_WEBHOOK_EVENT_ID_PREFIX,
        SERVICE_ID_PREFIX,
        PAYMENT_TRANSACTION_ID_PREFIX,
    }:
        raise ValueError("Unsupported MeterGate ID prefix")

    timestamp_ms, randomness = _next_payload()
    payload = (timestamp_ms << 80) | randomness
    return f"{prefix}{_encode_crockford(payload)}"


def new_account_id() -> str:
    return generate_id(ACCOUNT_ID_PREFIX)


def new_approval_identity_id() -> str:
    return generate_id(APPROVAL_IDENTITY_ID_PREFIX)


def new_approval_challenge_id() -> str:
    return generate_id(APPROVAL_CHALLENGE_ID_PREFIX)


def new_authorization_id() -> str:
    return generate_id(AUTHORIZATION_ID_PREFIX)


def new_merchant_id() -> str:
    return generate_id(MERCHANT_ID_PREFIX)


def new_payment_attempt_id() -> str:
    return generate_id(PAYMENT_ATTEMPT_ID_PREFIX)


def new_passkey_credential_id() -> str:
    return generate_id(PASSKEY_CREDENTIAL_ID_PREFIX)


def new_policy_id() -> str:
    return generate_id(POLICY_ID_PREFIX)


def new_payment_transaction_event_id() -> str:
    return generate_id(PAYMENT_TRANSACTION_EVENT_ID_PREFIX)


def new_policy_evaluation_id() -> str:
    return generate_id(POLICY_EVALUATION_ID_PREFIX)


def new_quote_id() -> str:
    return generate_id(QUOTE_ID_PREFIX)


def new_razorpay_webhook_event_id() -> str:
    return generate_id(RAZORPAY_WEBHOOK_EVENT_ID_PREFIX)


def new_service_id() -> str:
    return generate_id(SERVICE_ID_PREFIX)


def new_payment_transaction_id() -> str:
    return generate_id(PAYMENT_TRANSACTION_ID_PREFIX)


def _next_payload() -> tuple[int, int]:
    global _last_randomness, _last_timestamp_ms

    timestamp_ms = time.time_ns() // 1_000_000
    if not 0 <= timestamp_ms <= _MAX_TIMESTAMP:
        raise OverflowError("Current timestamp is outside the 48-bit ID range")

    with _generator_lock:
        if timestamp_ms > _last_timestamp_ms:
            randomness = secrets.randbits(80)
        else:
            timestamp_ms = _last_timestamp_ms
            if _last_randomness == _MAX_RANDOMNESS:
                if timestamp_ms == _MAX_TIMESTAMP:
                    raise OverflowError("MeterGate ID space exhausted")
                timestamp_ms += 1
                randomness = secrets.randbits(80)
            else:
                randomness = _last_randomness + 1

        _last_timestamp_ms = timestamp_ms
        _last_randomness = randomness
        return timestamp_ms, randomness


def _encode_crockford(value: int) -> str:
    characters = ["0"] * _ENCODED_LENGTH
    for index in range(_ENCODED_LENGTH - 1, -1, -1):
        value, remainder = divmod(value, 32)
        characters[index] = _CROCKFORD_ALPHABET[remainder]
    if value:
        raise OverflowError("ID payload exceeds its encoded length")
    return "".join(characters)
