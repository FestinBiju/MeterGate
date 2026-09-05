from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from app.cache.mcp import (
    McpRateLimiterUnavailable,
    McpRateLimitExceeded,
    RedisMcpRateLimiter,
)
from app.core.config import Settings
from app.domain.enums import AccountStatus, ApprovalIdentityStatus
from app.domain.exceptions import McpSessionForbiddenError, McpSessionUnauthorizedError
from app.domain.hashing import sha256_bytes
from app.domain.ids import new_account_id, new_approval_identity_id, new_mcp_agent_session_id
from app.models import Account, ApprovalIdentity, McpAgentSession, McpToolAuditEvent
from app.schemas.mcp import (
    ALL_MCP_SCOPES,
    McpAgentSessionCreate,
    McpExecuteResourceInput,
    McpPolicyInput,
    McpQuoteInput,
)
from app.services.mcp import (
    McpAgentSessionService,
    McpAuditService,
    ResolvedMcpSession,
)


class FakeAsyncSession:
    def __init__(
        self,
        *,
        scalar_values: Iterable[object | None] = (),
        get_value: object | None = None,
    ) -> None:
        self.scalar_values = iter(scalar_values)
        self.get_value = get_value
        self.added: list[object] = []
        self.commits = 0

    def add(self, value: object) -> None:
        if isinstance(value, McpAgentSession) and value.id is None:
            value.id = new_mcp_agent_session_id()
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def scalar(self, _statement: object) -> object | None:
        return next(self.scalar_values)

    async def get(self, _model: object, _identifier: str) -> object | None:
        return self.get_value


class FakeRedis:
    def __init__(self, result: object = (1, 60), error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.keys: list[str] = []

    async def eval(self, _script: str, _count: int, key: str, _window: int) -> object:
        self.keys.append(key)
        if self.error is not None:
            raise self.error
        return self.result


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/metergate",
        redis_url="redis://localhost:6379/15",
        _env_file=None,
    )


def _record(
    *,
    token: str,
    account_id: str,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    scopes: list[str] | None = None,
) -> McpAgentSession:
    now = datetime.now(UTC)
    return McpAgentSession(
        id=new_mcp_agent_session_id(),
        account_id=account_id,
        token_hash=sha256_bytes(token.encode()),
        scopes=scopes or list(ALL_MCP_SCOPES),
        created_at=now - timedelta(minutes=1),
        expires_at=expires_at or now + timedelta(minutes=5),
        revoked_at=revoked_at,
    )


def _account(account_id: str, status: AccountStatus = AccountStatus.ACTIVE) -> Account:
    return Account(id=account_id, display_name="MCP buyer", status=status)


def _identity(account_id: str) -> ApprovalIdentity:
    return ApprovalIdentity(
        id=new_approval_identity_id(),
        account_id=account_id,
        subject_ref=account_id,
        display_name="MCP buyer",
        webauthn_user_handle=b"a" * 32,
        status=ApprovalIdentityStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_agent_session_creation_persists_only_hash_and_returns_secret_once() -> None:
    account_id = new_account_id()
    database = FakeAsyncSession()
    service = McpAgentSessionService(database, _settings())  # type: ignore[arg-type]

    created = await service.create(
        McpAgentSessionCreate(
            scopes=["mcp:catalog.read"],
            expires_in_seconds=900,
            human_presence_proof_id="hpp_00000000000000000000000001",
        ),
        account_id=account_id,
    )

    record = database.added[0]
    assert isinstance(record, McpAgentSession)
    assert created.token.startswith("mcp_")
    assert record.token_hash == sha256_bytes(created.token.encode())
    assert created.token not in repr(record.__dict__)
    assert not hasattr(record, "token")
    assert database.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revoked_at", "expires_at", "expected_code"),
    [
        (datetime.now(UTC), None, "MCP_SESSION_REVOKED"),
        (None, datetime.now(UTC) - timedelta(seconds=1), "MCP_SESSION_EXPIRED"),
    ],
)
async def test_revoked_and_expired_agent_sessions_are_denied(
    revoked_at: datetime | None,
    expires_at: datetime | None,
    expected_code: str,
) -> None:
    token = "mcp_" + "a" * 43
    account_id = new_account_id()
    record = _record(
        token=token,
        account_id=account_id,
        revoked_at=revoked_at,
        expires_at=expires_at,
    )
    database = FakeAsyncSession(scalar_values=[record])
    service = McpAgentSessionService(database, _settings())  # type: ignore[arg-type]

    with pytest.raises(McpSessionUnauthorizedError) as raised:
        await service.resolve(token, required_scope="mcp:catalog.read")

    assert raised.value.reason_code == expected_code


@pytest.mark.asyncio
async def test_disabled_account_and_missing_scope_fail_closed() -> None:
    token = "mcp_" + "b" * 43
    account_id = new_account_id()
    record = _record(token=token, account_id=account_id, scopes=["mcp:catalog.read"])
    disabled_db = FakeAsyncSession(
        scalar_values=[record],
        get_value=_account(account_id, AccountStatus.DISABLED),
    )
    with pytest.raises(McpSessionUnauthorizedError) as disabled:
        await McpAgentSessionService(disabled_db, _settings()).resolve(  # type: ignore[arg-type]
            token, required_scope="mcp:catalog.read"
        )
    assert disabled.value.reason_code == "MCP_SESSION_REVOKED"

    active_db = FakeAsyncSession(
        scalar_values=[record, _identity(account_id)],
        get_value=_account(account_id),
    )
    with pytest.raises(McpSessionForbiddenError) as denied:
        await McpAgentSessionService(active_db, _settings()).resolve(  # type: ignore[arg-type]
            token, required_scope="mcp:quote.create"
        )
    assert denied.value.reason_code == "MCP_SCOPE_DENIED"


@pytest.mark.asyncio
async def test_mcp_audit_is_bounded_and_drops_all_secret_fields() -> None:
    account_id = new_account_id()
    record = _record(token="mcp_" + "c" * 43, account_id=account_id)
    principal = ResolvedMcpSession(record, _account(account_id), account_id)
    database = FakeAsyncSession()

    await McpAuditService(database).record(  # type: ignore[arg-type]
        principal,
        tool_name="execute_paid_resource",
        correlation_id="mcp-test",
        resource_ids={
            "execution_id": "ful_00000000000000000000000000",
            "capability": "capability-secret",
            "token": "mcp-secret",
            "razorpay_secret": "provider-secret",
        },
        result_code="FULFILLMENT_SUCCEEDED",
    )

    event = database.added[0]
    assert isinstance(event, McpToolAuditEvent)
    assert event.resource_ids == {"execution_id": "ful_00000000000000000000000000"}
    assert "secret" not in repr(event.resource_ids)


@pytest.mark.asyncio
async def test_redis_rate_limit_is_scoped_and_fails_closed() -> None:
    limited = FakeRedis(result=(11, 7))
    limiter = RedisMcpRateLimiter(limited)  # type: ignore[arg-type]
    with pytest.raises(McpRateLimitExceeded) as raised:
        await limiter.require(
            account_id="acct_example",
            agent_session_id="mas_example",
            action="request_quote",
            limit=10,
            window_seconds=60,
        )
    assert raised.value.retry_after_seconds == 7
    assert limited.keys == ["metergate:mcp-rate-limit:v1:acct_example:mas_example:request_quote"]

    unavailable = RedisMcpRateLimiter(  # type: ignore[arg-type]
        FakeRedis(error=RedisConnectionError("private infrastructure detail"))
    )
    with pytest.raises(McpRateLimiterUnavailable):
        await unavailable.require(
            account_id="acct_example",
            agent_session_id="mas_example",
            action="request_quote",
            limit=10,
            window_seconds=60,
        )


@pytest.mark.parametrize(
    ("model", "payload", "forbidden_field"),
    [
        (
            McpQuoteInput,
            {"service_id": "svc_00000000000000000000000000", "input": {}, "amount": 1},
            "amount",
        ),
        (
            McpPolicyInput,
            {"maximum_amount": 500, "expires_in_seconds": 60, "subject_ref": "victim"},
            "subject_ref",
        ),
        (
            McpExecuteResourceInput,
            {
                "merchant_slug": "orbitintel",
                "service_slug": "orbital-risk-report",
                "input": {"norad_id": 25544},
                "capability": "capability",
                "force": True,
            },
            "force",
        ),
    ],
)
def test_mcp_schemas_reject_authority_overrides(
    model: type[Any], payload: dict[str, Any], forbidden_field: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        model.model_validate(payload)
    assert forbidden_field in str(raised.value)


def test_mcp_surface_has_no_approval_payment_refund_or_operator_mutations(make_client) -> None:
    client = make_client()
    schema = client.get("/openapi.json").json()
    mcp_paths = sorted(path for path in schema["paths"] if path.startswith("/api/v1/mcp"))

    assert len(mcp_paths) == 13
    assert len([path for path in mcp_paths if "/tools/" in path]) == 10
    assert "/api/v1/mcp/sessions/{agent_session_id}/renew" in mcp_paths
    forbidden_terms = ("approve", "passkey", "razorpay", "refund", "operator", "admin")
    assert all(not any(term in path for term in forbidden_terms) for path in mcp_paths)
    assert all(not scope.startswith(("operator.", "admin.")) for scope in ALL_MCP_SCOPES)


def test_mcp_json_inputs_reject_excessive_nesting() -> None:
    value: object = None
    for _ in range(34):
        value = [value]
    with pytest.raises(ValidationError, match="nesting"):
        McpQuoteInput.model_validate(
            {"service_id": "svc_00000000000000000000000000", "input": value}
        )
