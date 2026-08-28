"""Strict buyer-agent session and MCP orchestration contracts."""

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictInt, StringConstraints, field_validator

from app.schemas.catalog import CatalogResponse, CatalogServiceDetail
from app.schemas.common import APIModel
from app.schemas.entitlements import (
    CapabilityResponse,
    EntitlementLookupResponse,
    FulfillmentPendingResponse,
    FulfillmentResultResponse,
    PaymentRequiredResponse,
)
from app.schemas.human_presence import HumanPresenceProofId
from app.schemas.policies import BuyerPolicyCreate, BuyerPolicyResponse, PolicyEvaluationId
from app.schemas.policy_evaluations import PolicyEvaluationResponse
from app.schemas.quotes import QuoteCreate, QuoteResponse

McpScope = Literal[
    "mcp:catalog.read",
    "mcp:quote.create",
    "mcp:policy.create",
    "mcp:policy.evaluate",
    "mcp:commerce.read",
    "mcp:capability.issue",
    "mcp:resource.execute",
]
McpAgentSessionId = Annotated[
    str,
    StringConstraints(pattern=r"^mas_[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
]
McpBridgeToken = Annotated[
    str,
    StringConstraints(pattern=r"^mcp_[A-Za-z0-9_-]{43}$"),
]
CorrelationId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"),
]

ALL_MCP_SCOPES: tuple[McpScope, ...] = (
    "mcp:catalog.read",
    "mcp:quote.create",
    "mcp:policy.create",
    "mcp:policy.evaluate",
    "mcp:commerce.read",
    "mcp:capability.issue",
    "mcp:resource.execute",
)


class McpAgentSessionCreate(APIModel):
    scopes: Annotated[list[McpScope], Field(min_length=1, max_length=len(ALL_MCP_SCOPES))]
    expires_in_seconds: Annotated[StrictInt, Field(ge=60, le=3_600)]
    human_presence_proof_id: HumanPresenceProofId

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: list[McpScope]) -> list[McpScope]:
        if len(value) != len(set(value)):
            raise ValueError("MCP scopes must be unique")
        order = {scope: index for index, scope in enumerate(ALL_MCP_SCOPES)}
        return sorted(value, key=order.__getitem__)


class McpAgentSessionResponse(APIModel):
    id: McpAgentSessionId
    account_id: str
    scopes: list[McpScope]
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    state: Literal["active", "expired", "revoked"]


class McpAgentSessionCreated(McpAgentSessionResponse):
    reason_code: Literal["MCP_SESSION_CREATED"] = "MCP_SESSION_CREATED"
    token: McpBridgeToken


class McpAgentSessionList(APIModel):
    sessions: list[McpAgentSessionResponse]


class McpEmptyInput(APIModel):
    pass


class McpGetServiceInput(APIModel):
    service_id: Annotated[str, StringConstraints(pattern=r"^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]


class McpResourceInput(APIModel):
    merchant_slug: Annotated[
        str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    ]
    service_slug: Annotated[
        str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    ]
    input: JsonValue

    @field_validator("input")
    @classmethod
    def bound_input(cls, value: JsonValue) -> JsonValue:
        _validate_bounded_json(value)
        return value


class McpQuoteInput(QuoteCreate):
    @field_validator("input")
    @classmethod
    def bound_input(cls, value: JsonValue) -> JsonValue:
        _validate_bounded_json(value)
        return value


class McpPolicyInput(BuyerPolicyCreate):
    allowed_purchase_types: Annotated[
        list[Literal["one_time"]],
        Field(min_length=1, max_length=1),
    ]


class McpEvaluateQuoteInput(APIModel):
    policy_id: Annotated[str, StringConstraints(pattern=r"^pol_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
    quote_id: Annotated[str, StringConstraints(pattern=r"^qte_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]


class McpEvaluationResult(APIModel):
    evaluation: PolicyEvaluationResponse
    state: Literal["denied", "human_approval_required"]
    reason_code: str
    approval_url: str | None


class McpPurchaseStatusInput(APIModel):
    evaluation_id: PolicyEvaluationId


McpPurchaseState = Literal[
    "evaluation_denied",
    "human_approval_required",
    "authorized",
    "payment_ready",
    "payment_pending",
    "payment_failed",
    "paid",
    "entitlement_preparing",
    "access_ready",
    "fulfilled",
    "compensation_pending",
    "manual_review",
    "refunded",
]


class McpPurchaseStatusResponse(APIModel):
    evaluation_id: PolicyEvaluationId
    state: McpPurchaseState
    reason_code: str
    retry_after_seconds: int | None = None
    approval_url: str | None = None
    payment_url: str | None = None
    authorization_id: str | None = None
    transaction_id: str | None = None
    entitlement_id: str | None = None
    execution_id: str | None = None
    result_hash: str | None = None
    replayed_result: bool | None = None


class McpEntitlementInput(APIModel):
    transaction_id: Annotated[str, StringConstraints(pattern=r"^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]


class McpCapabilityInput(APIModel):
    entitlement_id: Annotated[str, StringConstraints(pattern=r"^ent_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]


class McpExecuteResourceInput(McpResourceInput):
    capability: Annotated[str, StringConstraints(min_length=1, max_length=4096)]


McpToolResponse = (
    CatalogResponse
    | CatalogServiceDetail
    | PaymentRequiredResponse
    | QuoteResponse
    | BuyerPolicyResponse
    | McpEvaluationResult
    | McpPurchaseStatusResponse
    | EntitlementLookupResponse
    | CapabilityResponse
    | FulfillmentPendingResponse
    | FulfillmentResultResponse
)


def _validate_bounded_json(value: JsonValue) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("MCP input must be standard JSON") from error
    if len(encoded) > 262_144:
        raise ValueError("MCP input exceeds 256 KiB")
    pending: list[tuple[JsonValue, int]] = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 10_000 or depth > 32:
            raise ValueError("MCP input nesting or item count exceeds the safe limit")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
