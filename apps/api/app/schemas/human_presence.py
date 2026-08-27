"""Strict public contracts for proof of human presence."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints

from app.schemas.approvals import BrowserCredential
from app.schemas.common import APIModel

HumanPresenceChallengeId = Annotated[
    str, StringConstraints(pattern=r"^hpc_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
]
HumanPresenceProofId = Annotated[
    str, StringConstraints(pattern=r"^hpp_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
]


class HumanPresenceChallengeCreate(APIModel):
    action_class: Literal["new_agent_session"]
    resource_binding: Annotated[dict[str, JsonValue], Field(min_length=2, max_length=2)]


class HumanPresenceChallengeResponse(APIModel):
    challenge_id: HumanPresenceChallengeId
    action_class: Literal["new_agent_session"]
    resource_binding: dict[str, JsonValue]
    public_key: dict[str, JsonValue]
    expires_at: datetime
    reason_code: Literal["HUMAN_PRESENCE_CHALLENGE_CREATED"] = "HUMAN_PRESENCE_CHALLENGE_CREATED"


class HumanPresenceVerify(APIModel):
    credential: BrowserCredential


class HumanPresenceProofResponse(APIModel):
    id: HumanPresenceProofId
    action_class: Literal["new_agent_session"]
    resource_binding: dict[str, JsonValue]
    issued_at: datetime
    expires_at: datetime
    state: Literal["active", "expired", "consumed"]
    presence_version: Literal["1"]
    presence_hash: str
    reason_code: Literal["HUMAN_PRESENCE_VERIFIED"] = "HUMAN_PRESENCE_VERIFIED"


class HumanPresenceStatusResponse(APIModel):
    proofs: list[HumanPresenceProofResponse]
