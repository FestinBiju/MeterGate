"""Versioned integrity and deterministic risk rules for human-presence proofs."""

from datetime import datetime
from typing import Any, Literal

from app.domain.hashing import canonical_utc_datetime, sha256_json

HUMAN_PRESENCE_VERSION = "1"
HumanPresenceAction = Literal["new_agent_session"]
_MCP_BUYER_SCOPES = frozenset(
    {
        "mcp:catalog.read",
        "mcp:quote.create",
        "mcp:policy.create",
        "mcp:policy.evaluate",
        "mcp:commerce.read",
        "mcp:capability.issue",
        "mcp:resource.execute",
    }
)


def normalize_resource_binding(
    action_class: HumanPresenceAction,
    resource_binding: dict[str, Any],
) -> dict[str, Any]:
    """Reject client-invented scope and normalize the supported action binding."""
    if action_class != "new_agent_session" or set(resource_binding) != {
        "scopes",
        "expires_in_seconds",
    }:
        raise ValueError("Human-presence action or resource binding is not supported")
    scopes = resource_binding["scopes"]
    ttl = resource_binding["expires_in_seconds"]
    if (
        not isinstance(scopes, list)
        or not scopes
        or len(scopes) > 7
        or any(not isinstance(scope, str) for scope in scopes)
        or len(scopes) != len(set(scopes))
        or any(scope not in _MCP_BUYER_SCOPES for scope in scopes)
        or isinstance(ttl, bool)
        or not isinstance(ttl, int)
        or not 60 <= ttl <= 86_400
    ):
        raise ValueError("Human-presence resource binding is invalid")
    return {"scopes": sorted(scopes), "expires_in_seconds": ttl}


def resource_binding_hash(action_class: HumanPresenceAction, binding: dict[str, Any]) -> str:
    return sha256_json(
        {
            "version": HUMAN_PRESENCE_VERSION,
            "action_class": action_class,
            "resource_binding": normalize_resource_binding(action_class, binding),
        }
    )


def calculate_presence_hash(
    *,
    proof_id: str,
    account_id: str,
    session_id_hash: str,
    passkey_credential_id: str,
    action_class: HumanPresenceAction,
    resource_binding: dict[str, Any],
    origin: str,
    issued_at: datetime,
    expires_at: datetime,
    challenge_hash: str,
) -> str:
    return sha256_json(
        {
            "version": HUMAN_PRESENCE_VERSION,
            "proof_id": proof_id,
            "account_id": account_id,
            "session_id_hash": session_id_hash,
            "passkey_credential_id": passkey_credential_id,
            "action_class": action_class,
            "resource_binding": normalize_resource_binding(action_class, resource_binding),
            "origin": origin,
            "issued_at": canonical_utc_datetime(issued_at),
            "expires_at": canonical_utc_datetime(expires_at),
            "challenge_hash": challenge_hash,
        }
    )


def requires_human_presence(action_class: HumanPresenceAction) -> bool:
    """Deterministic, deliberately small Milestone 11 risk gate."""
    return action_class == "new_agent_session"
