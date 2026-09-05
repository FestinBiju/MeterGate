"use client";

import { useCallback, useEffect, useState } from "react";

import { useAccountSession } from "@/components/account-session";
import { ApiRequestFailure } from "@/lib/api-client";
import { HumanPresenceGate } from "@/components/human-presence-gate";

const allScopes = [
  "mcp:catalog.read",
  "mcp:quote.create",
  "mcp:policy.create",
  "mcp:policy.evaluate",
  "mcp:commerce.read",
  "mcp:capability.issue",
  "mcp:resource.execute",
] as const;

type AgentSession = {
  id: string;
  scopes: string[];
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  state: "active" | "expired" | "revoked";
};

type Failure = { code: string; message: string };

const dateTime = new Intl.DateTimeFormat("en-IN", {
  dateStyle: "medium",
  timeStyle: "short",
});

export function AgentConnections() {
  const { state } = useAccountSession();
  if (state.kind !== "authenticated") return null;
  return <AccountAgentConnections key={state.session.account.id} />;
}

function AccountAgentConnections() {
  const { apiBaseEndpoint, state, sessionRevision, requestAuthenticated } =
    useAccountSession();
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [selectedScopes, setSelectedScopes] = useState<string[]>([...allScopes]);
  const [token, setToken] = useState<string | null>(null);
  const [lifetime, setLifetime] = useState(3600);
  const [notice, setNotice] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<
    { kind: "create" } | { kind: "renew"; session: AgentSession } | { kind: "revoke"; sessionId: string } | null
  >(null);
  const [busy, setBusy] = useState(false);
  const [humanBusy, setHumanBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);

  const refresh = useCallback(async () => {
    if (!apiBaseEndpoint || state.kind !== "authenticated") return;
    try {
      const body = await requestAuthenticated(`${apiBaseEndpoint}/mcp/sessions`, {
        method: "GET",
      });
      setSessions(parseSessions(body));
      setFailure(null);
    } catch (error) {
      setFailure(toFailure(error, "MCP_SESSION_LIST_FAILED"));
    }
  }, [apiBaseEndpoint, requestAuthenticated, state.kind]);

  useEffect(() => {
    const timeout = window.setTimeout(() => void refresh(), 0);
    const interval = window.setInterval(() => void refresh(), 30_000);
    return () => { window.clearTimeout(timeout); window.clearInterval(interval); };
  }, [refresh, sessionRevision]);

  if (state.kind !== "authenticated" || !apiBaseEndpoint) return null;

  const createSession = async (humanPresenceProofId: string) => {
    if (busy || selectedScopes.length === 0) return;
    setPendingAction(null);
    setBusy(true);
    setFailure(null);
    setNotice(null);
    setToken(null);
    try {
      const body = await requestAuthenticated(`${apiBaseEndpoint}/mcp/sessions`, {
        method: "POST",
        body: {
          scopes: selectedScopes,
          expires_in_seconds: lifetime,
          human_presence_proof_id: humanPresenceProofId,
        },
      });
      if (typeof body.token !== "string" || !body.token.startsWith("mcp_")) {
        throw new Error("Agent session token response was invalid");
      }
      setToken(body.token);
      await refresh();
    } catch (error) {
      setFailure(toFailure(error, "MCP_SESSION_CREATE_FAILED"));
    } finally {
      setBusy(false);
    }
  };

  const renew = async (sessionId: string, humanPresenceProofId: string) => {
    if (busy) return;
    setPendingAction(null);
    setBusy(true);
    setFailure(null);
    setNotice(null);
    try {
      const body = await requestAuthenticated(
        `${apiBaseEndpoint}/mcp/sessions/${encodeURIComponent(sessionId)}/renew`,
        { method: "POST", body: { expires_in_seconds: lifetime, human_presence_proof_id: humanPresenceProofId } },
      );
      if (!isSession(body) || body.id !== sessionId || body.state !== "active") {
        throw new Error("Agent renewal response was invalid");
      }
      await refresh();
      setNotice("Access renewed. Your existing MCP key and client configuration still work. Retry the agent's last tool call.");
    } catch (error) {
      setFailure(toFailure(error, "MCP_SESSION_RENEW_FAILED"));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (sessionId: string) => {
    if (busy) return;
    setPendingAction(null);
    setBusy(true);
    setFailure(null);
    try {
      await requestAuthenticated(
        `${apiBaseEndpoint}/mcp/sessions/${encodeURIComponent(sessionId)}/revoke`,
        { method: "POST", body: {} },
      );
      await refresh();
    } catch (error) {
      setFailure(toFailure(error, "MCP_SESSION_REVOKE_FAILED"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section id="agent-connections" className="mt-10 rounded-3xl border border-cyan-300/15 bg-cyan-300/[0.025] p-5 sm:p-6" aria-labelledby="agent-connections-heading">
      <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-cyan-200/70">
        Buyer-controlled agent access
      </p>
      <h2 id="agent-connections-heading" className="mt-2 text-xl font-semibold text-white">
        Agent Connections
      </h2>
      <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
        Configure your MCP client once. Use the same connection across conversations, and renew its access here with a passkey when it expires. Renewal keeps the existing key and scopes; purchases still require their own human approval.
      </p>
      <p className="mt-2 text-xs leading-5 text-slate-400">
        Revoke a connection if its key is lost or exposed. Revocation is permanent; renewal only works for connections owned by this signed-in account.
      </p>

      <label className="mt-4 block text-xs font-semibold text-slate-300">
        Access duration
        <select value={lifetime} disabled={busy || pendingAction !== null} onChange={(event) => setLifetime(Number(event.target.value))} className="ml-3 rounded-lg border border-white/15 bg-slate-950 px-3 py-2 text-slate-100">
          <option value={900}>15 minutes</option>
          <option value={1800}>30 minutes</option>
          <option value={3600}>1 hour</option>
        </select>
      </label>

      <fieldset disabled={busy || pendingAction !== null} className="mt-5 grid gap-2 sm:grid-cols-2">
        <legend className="mb-2 text-xs font-semibold text-slate-300">Session scopes</legend>
        {allScopes.map((scope) => (
          <label key={scope} className="flex items-center gap-2 rounded-lg border border-white/[0.07] px-3 py-2 text-xs text-slate-300">
            <input
              type="checkbox"
              checked={selectedScopes.includes(scope)}
              onChange={(event) =>
                setSelectedScopes((current) =>
                  event.target.checked
                    ? [...current, scope]
                    : current.filter((item) => item !== scope),
                )
              }
            />
            <span className="font-mono">{scope}</span>
          </label>
        ))}
      </fieldset>

      <button
        type="button"
        disabled={busy || pendingAction !== null || selectedScopes.length === 0}
        onClick={() => setPendingAction({ kind: "create" })}
        className="mt-4 rounded-xl border border-cyan-300/25 bg-cyan-300/[0.08] px-4 py-2.5 text-sm font-semibold text-cyan-100 disabled:opacity-50"
      >
        {busy ? "Updating agent sessions…" : "Create Agent Session"}
      </button>
      <p className="mt-2 text-xs text-slate-400">Create a new session only when connecting a new client or replacing a revoked key. To resume an existing client, use Renew access below.</p>

      {pendingAction ? (
        <div role="dialog" aria-modal="true" aria-labelledby="agent-session-confirmation" className="mt-4 rounded-2xl border border-amber-300/25 bg-amber-300/[0.06] p-4">
          <h3 id="agent-session-confirmation" className="text-sm font-semibold text-amber-100">
            Explicit confirmation required
          </h3>
          <p className="mt-2 text-xs leading-5 text-slate-300">
            {pendingAction.kind === "create"
              ? `Create a ${lifetime / 60}-minute buyer-agent credential with the displayed scopes? It cannot approve purchases, operate Razorpay, issue refunds, or access operator APIs.`
              : pendingAction.kind === "renew"
                ? `Renew connection ${pendingAction.session.id} for ${lifetime / 60} minutes with its existing scopes? The key already stored in your MCP client will work again.`
                : `Revoke agent session ${pendingAction.sessionId} permanently? Every later scoped tool call using it will fail and this key cannot be renewed.`}
          </p>
          <div className="mt-3 flex gap-2">
            {pendingAction.kind === "create" ? (
              <HumanPresenceGate
                buttonLabel="Confirm Agent Session with passkey"
                disabled={busy}
                resourceBinding={{ scopes: selectedScopes, expires_in_seconds: lifetime }}
                onVerified={createSession}
                onBusyChange={setHumanBusy}
              />
            ) : pendingAction.kind === "renew" ? (
              <HumanPresenceGate
                actionClass="renew_agent_session"
                buttonLabel="Renew access with passkey"
                disabled={busy}
                resourceBinding={{ agent_session_id: pendingAction.session.id, scopes: pendingAction.session.scopes, expires_in_seconds: lifetime }}
                onVerified={(proofId) => renew(pendingAction.session.id, proofId)}
                onBusyChange={setHumanBusy}
              />
            ) : (
              <button type="button" disabled={busy} onClick={() => void revoke(pendingAction.sessionId)} className="rounded-lg border border-amber-200/25 px-3 py-2 text-xs font-semibold text-amber-100 disabled:opacity-50">
                Confirm Revocation
              </button>
            )}
            <button type="button" disabled={busy || humanBusy} onClick={() => setPendingAction(null)} className="rounded-lg border border-white/10 px-3 py-2 text-xs text-slate-300 disabled:opacity-50">
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {token ? (
        <div role="status" className="mt-5 rounded-2xl border border-amber-300/25 bg-amber-300/[0.06] p-4">
          <p className="text-xs font-semibold text-amber-100">Copy this credential now</p>
          <p className="mt-1 text-xs leading-5 text-slate-300">
            It is shown once and MeterGate stores only its SHA-256 hash. Dismiss it after configuring the local client.
          </p>
          <code className="mt-3 block break-all rounded-lg bg-black/30 p-3 text-xs text-amber-100">{token}</code>
          <div className="mt-3 flex gap-2">
            <button type="button" onClick={() => void navigator.clipboard.writeText(token)} className="rounded-lg border border-white/10 px-3 py-2 text-xs text-white">
              Copy token
            </button>
            <button type="button" onClick={() => setToken(null)} className="rounded-lg border border-white/10 px-3 py-2 text-xs text-slate-300">
              Dismiss secret
            </button>
          </div>
        </div>
      ) : null}

      {failure ? (
        <div role="alert" className="mt-4 rounded-xl border border-rose-300/20 bg-rose-300/[0.05] p-3 text-xs text-rose-100">
          <span className="font-mono">{failure.code}</span> · {failure.message}
        </div>
      ) : null}
      {notice ? <p role="status" className="mt-4 text-sm text-emerald-200">{notice}</p> : null}

      <div className="mt-6 space-y-3">
        {sessions.length === 0 ? (
          <p className="text-xs text-slate-500">No agent sessions have been created.</p>
        ) : (
          sessions.map((session) => (
            <article id={`agent-session-${session.id}`} key={session.id} className="scroll-mt-6 rounded-xl border border-white/[0.07] bg-black/15 p-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <p className="font-mono text-xs text-cyan-100">{session.id}</p>
                  <p className="mt-1 text-xs text-slate-400">
                    Created {dateTime.format(new Date(session.created_at))} · expires {dateTime.format(new Date(session.expires_at))}
                  </p>
                  <p className="mt-1 text-xs text-slate-500">
                    Last used {session.last_used_at ? dateTime.format(new Date(session.last_used_at)) : "never"} · {session.state}
                  </p>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {session.scopes.map((scope) => (
                      <span key={scope} className="rounded-full border border-white/10 px-2 py-1 font-mono text-[9px] text-slate-400">{scope}</span>
                    ))}
                  </div>
                </div>
                {session.state !== "revoked" ? (
                  <div className="flex flex-wrap gap-2">
                  <button type="button" disabled={busy || pendingAction !== null} onClick={() => setPendingAction({ kind: "renew", session })} className="rounded-lg border border-emerald-300/25 px-3 py-2 text-xs text-emerald-100 disabled:opacity-50">
                    Renew access
                  </button>
                  <button type="button" disabled={busy || pendingAction !== null} onClick={() => setPendingAction({ kind: "revoke", sessionId: session.id })} className="rounded-lg border border-rose-300/20 px-3 py-2 text-xs text-rose-100 disabled:opacity-50">
                    Revoke Agent Session
                  </button>
                  </div>
                ) : null}
              </div>
            </article>
          ))
        )}
      </div>
    </section>
  );
}

function parseSessions(value: Record<string, unknown>): AgentSession[] {
  if (!Array.isArray(value.sessions)) throw new Error("Agent session list response was invalid");
  return value.sessions.filter(isSession);
}

function isSession(value: unknown): value is AgentSession {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.id === "string" &&
    Array.isArray(item.scopes) &&
    item.scopes.every((scope) => typeof scope === "string") &&
    typeof item.created_at === "string" &&
    typeof item.expires_at === "string" &&
    (item.last_used_at === null || typeof item.last_used_at === "string") &&
    (item.revoked_at === null || typeof item.revoked_at === "string") &&
    ["active", "expired", "revoked"].includes(String(item.state))
  );
}

function toFailure(error: unknown, fallback: string): Failure {
  return error instanceof ApiRequestFailure
    ? { code: error.code, message: error.message }
    : { code: fallback, message: "The agent-session operation failed safely." };
}
