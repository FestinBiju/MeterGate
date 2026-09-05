"""Renew the configured bridge without returning a new secret or bypassing passkeys."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import Response
from pydantic import ValidationError
from sqlalchemy import select

from app.api.v1.dependencies import require_current_account
from app.api.v1.mcp import renew_agent_session
from app.core.config import Settings
from app.domain.exceptions import (
    AuthenticationConflictError,
    AuthenticationForbiddenError,
    McpSessionConflictError,
    McpSessionNotFoundError,
    McpSessionUnauthorizedError,
)
from app.domain.hashing import sha256_bytes
from app.domain.human_presence import calculate_presence_hash, normalize_resource_binding
from app.domain.ids import new_human_presence_proof_id
from app.models import HumanPresenceProof, McpAgentSession, McpToolAuditEvent
from app.schemas.human_presence import HumanPresenceChallengeCreate
from app.schemas.mcp import McpAgentSessionCreate, McpAgentSessionRenew
from app.services.human_presence import HumanPresenceService
from app.services.mcp import McpAgentSessionService
from tests.test_postgresql_domain import (
    _drop_isolated_schema,
    _prepare_isolated_database,
    create_authenticated_account,
)

SESSION_ID = "mas_00000000000000000000000001"
PROOF_ID = "hpp_00000000000000000000000001"
ORIGIN = "http://localhost:3000"
BROWSER_SESSION = "ses_" + "x" * 43


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer mcp_" + "x" * 43}])
def test_agent_token_and_anonymous_caller_cannot_renew(make_client, headers):
    response = make_client().post(
        f"/api/v1/mcp/sessions/{SESSION_ID}/renew",
        json={"expires_in_seconds": 900, "human_presence_proof_id": PROOF_ID},
        headers=headers,
    )
    assert response.status_code == 401
    assert response.json()["reason_code"] == "AUTH_SESSION_REQUIRED"


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({}, "AUTH_ORIGIN_NOT_ALLOWED"),
        ({"Origin": "https://untrusted.example"}, "AUTH_ORIGIN_NOT_ALLOWED"),
        ({"Origin": ORIGIN}, "AUTH_CSRF_REQUIRED"),
        ({"Origin": ORIGIN, "X-CSRF-Token": "wrong"}, "AUTH_CSRF_INVALID"),
    ],
)
def test_renewal_requires_origin_and_csrf(make_client, headers, reason):
    client = make_client()
    client.app.dependency_overrides[require_current_account] = lambda: SimpleNamespace(
        state=SimpleNamespace(csrf_token="expected")
    )
    response = client.post(
        f"/api/v1/mcp/sessions/{SESSION_ID}/renew",
        json={"expires_in_seconds": 900, "human_presence_proof_id": PROOF_ID},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["reason_code"] == reason


def test_renewal_contract_cannot_change_scopes_token_owner_or_unbounded_ttl():
    payload = {"expires_in_seconds": 900, "human_presence_proof_id": PROOF_ID}
    for key, value in {
        "scopes": ["mcp:resource.execute"],
        "token": "replacement",
        "account_id": "other",
        "expires_in_seconds": 3601,
    }.items():
        with pytest.raises(ValidationError):
            McpAgentSessionRenew.model_validate({**payload, key: value})
    with pytest.raises(ValidationError):
        McpAgentSessionRenew.model_validate({"expires_in_seconds": 900})


def test_presence_contract_binds_renewal_to_exact_connection():
    binding = {
        "agent_session_id": SESSION_ID,
        "scopes": ["mcp:catalog.read"],
        "expires_in_seconds": 900,
    }
    assert normalize_resource_binding("renew_agent_session", binding) == binding
    for action, value in [
        ("new_agent_session", binding),
        (
            "renew_agent_session",
            {key: value for key, value in binding.items() if key != "agent_session_id"},
        ),
        ("renew_agent_session", {**binding, "agent_session_id": "invalid"}),
        ("renew_agent_session", {**binding, "extra": True}),
    ]:
        with pytest.raises(ValidationError):
            HumanPresenceChallengeCreate(action_class=action, resource_binding=value)


def test_postgresql_renewal_preserves_key_and_atomically_consumes_exact_proof():
    async def run():
        settings = Settings(
            database_url="postgresql://unused:unused@localhost/unused",
            redis_url="redis://localhost:6379/15",
            _env_file=None,
        )
        isolated = await _prepare_isolated_database()
        try:
            owner, credential = await create_authenticated_account(
                isolated, display_name="Renewal test", raw_credential_id=b"renewal-test"
            )
            async with isolated.database.session() as session:
                created = await McpAgentSessionService(session, settings).create(
                    McpAgentSessionCreate(
                        scopes=["mcp:catalog.read"],
                        expires_in_seconds=900,
                        human_presence_proof_id=PROOF_ID,
                    ),
                    account_id=owner.account.id,
                )
                record = await session.get(McpAgentSession, created.id)
                record.created_at = datetime.now(UTC) - timedelta(minutes=20)
                record.expires_at = datetime.now(UTC) - timedelta(minutes=5)
                await session.commit()
            now = datetime.now(UTC)
            binding = {
                "agent_session_id": created.id,
                "scopes": ["mcp:catalog.read"],
                "expires_in_seconds": 900,
            }
            proof_id = new_human_presence_proof_id()
            proof_fields = dict(
                account_id=owner.account.id,
                session_id_hash=sha256_bytes(BROWSER_SESSION.encode()),
                passkey_credential_id=credential.id,
                action_class="renew_agent_session",
                resource_binding=binding,
                origin=ORIGIN,
                issued_at=now,
                expires_at=now + timedelta(minutes=2),
                challenge_hash=sha256_bytes(b"renewal-proof"),
            )
            async with isolated.database.session() as session:
                session.add(
                    HumanPresenceProof(
                        id=proof_id,
                        **proof_fields,
                        presence_version="1",
                        presence_hash=calculate_presence_hash(proof_id=proof_id, **proof_fields),
                    )
                )
                await session.commit()
            payload = McpAgentSessionRenew(expires_in_seconds=900, human_presence_proof_id=proof_id)
            async with isolated.database.session() as session:
                with pytest.raises(McpSessionUnauthorizedError, match="Renew this connection"):
                    await McpAgentSessionService(session, settings).resolve(created.token)
            async with isolated.database.session() as session:
                with pytest.raises(McpSessionNotFoundError):
                    await McpAgentSessionService(session, settings).get_renewable(
                        created.id, account_id="other"
                    )
            # Scope/action/session changes cannot consume the proof.
            for action, changed in [
                (
                    "new_agent_session",
                    {k: v for k, v in binding.items() if k != "agent_session_id"},
                ),
                ("renew_agent_session", {**binding, "agent_session_id": SESSION_ID}),
                ("renew_agent_session", {**binding, "expires_in_seconds": 3600}),
                ("renew_agent_session", {**binding, "scopes": ["mcp:resource.execute"]}),
            ]:
                async with isolated.database.session() as session:
                    with pytest.raises(AuthenticationForbiddenError):
                        await HumanPresenceService(
                            session, None, None, settings
                        ).require_and_consume(
                            proof_id,
                            account_id=owner.account.id,
                            session_id=BROWSER_SESSION,
                            origin=ORIGIN,
                            action_class=action,
                            resource_binding=changed,
                        )
            # A rejected lifetime rolls back both grant and proof consumption.
            async with isolated.database.session() as session:
                service = McpAgentSessionService(
                    session, settings.model_copy(update={"mcp_agent_session_max_ttl_seconds": 60})
                )
                record = await service.get_renewable(created.id, account_id=owner.account.id)
                await HumanPresenceService(session, None, None, settings).require_and_consume(
                    proof_id,
                    account_id=owner.account.id,
                    session_id=BROWSER_SESSION,
                    origin=ORIGIN,
                    action_class="renew_agent_session",
                    resource_binding=binding,
                )
                with pytest.raises(McpSessionConflictError):
                    await service.renew(record, payload)
                await session.rollback()
            async with isolated.database.session() as session:
                assert (await session.get(HumanPresenceProof, proof_id)).consumed_at is None
                service = McpAgentSessionService(session, settings)
                record = await service.get_renewable(created.id, account_id=owner.account.id)
                old_hash = record.token_hash
                response = Response()
                renewed = await renew_agent_session(
                    created.id,
                    payload,
                    response,
                    service,
                    SimpleNamespace(
                        account=owner.account, state=SimpleNamespace(session_id=BROWSER_SESSION)
                    ),
                    ORIGIN,
                    HumanPresenceService(session, None, None, settings),
                )
                assert "no-store" in response.headers["cache-control"]
                assert renewed.state == "active"
                assert "token" not in renewed.model_dump()
                assert record.token_hash == old_hash == sha256_bytes(created.token.encode())
                assert renewed.scopes == ["mcp:catalog.read"]
                assert 890 <= (renewed.expires_at - datetime.now(UTC)).total_seconds() <= 900
            # Fresh requests use the original configured token.
            async with isolated.database.session() as session:
                principal = await McpAgentSessionService(session, settings).resolve(
                    created.token, required_scope="mcp:catalog.read"
                )
                assert principal.account_id == owner.account.id
                audit = (
                    await session.scalars(
                        select(McpToolAuditEvent).where(
                            McpToolAuditEvent.agent_session_id == created.id
                        )
                    )
                ).one()
                assert audit.result_code == "MCP_SESSION_RENEWED"
                assert audit.resource_ids["human_presence_proof_id"] == proof_id
                assert created.token not in str(audit.resource_ids)
                with pytest.raises(AuthenticationConflictError):
                    await HumanPresenceService(session, None, None, settings).require_and_consume(
                        proof_id,
                        account_id=owner.account.id,
                        session_id=BROWSER_SESSION,
                        origin=ORIGIN,
                        action_class="renew_agent_session",
                        resource_binding=binding,
                    )
            # A preloaded identity map cannot conceal a later revocation.
            async with isolated.database.session() as stale:
                stale_record = await stale.get(McpAgentSession, created.id)
                assert stale_record.revoked_at is None
                async with isolated.database.session() as session:
                    await McpAgentSessionService(session, settings).revoke(
                        created.id, account_id=owner.account.id
                    )
                with pytest.raises(McpSessionConflictError):
                    await McpAgentSessionService(stale, settings).get_renewable(
                        created.id, account_id=owner.account.id
                    )
            async with isolated.database.session() as session:
                with pytest.raises(McpSessionUnauthorizedError) as revoked:
                    await McpAgentSessionService(session, settings).resolve(created.token)
                assert revoked.value.reason_code == "MCP_SESSION_REVOKED"
        finally:
            await isolated.database.dispose()
            await _drop_isolated_schema(isolated.database_url, isolated.schema)

    asyncio.run(run())
