"use client";

import { useCallback, useEffect, useState } from "react";
import { useAccountSession } from "@/components/account-session";
import { configuredApiBaseEndpoint } from "@/lib/api-client";
import { getPasskeyCredential } from "@/lib/webauthn";

type WorkItem = { type: string; severity: string; state: string; resource_id: string; transaction_id?: string; age_seconds: number; reason_code?: string };
type Worker = { worker_type: string; status: "healthy" | "stale"; last_heartbeat?: string; last_successful_work?: string; backlog_count: number; alert_code?: string };
type Health = { postgresql: string; redis: string; workers: Worker[]; queues: Record<string, number> };
type Transaction = { transaction_id: string; account_id: string; amount: number; currency: string; payment_state: string; created_at: string };
type Incident = { id: string; severity: string; state: string; reason_code: string };
type CaseDetail = {
  facts: {
    transaction_id: string; account_id: string; merchant_name?: string; service_name?: string;
    amount: number; currency: string; payment_state: string; provider_order_id?: string;
    provider_payment_id?: string; provider_payment_status?: string;
    fulfillment?: { id: string; state: string; attempt_count: number; failure_code?: string; compensation_required: boolean };
    compensation?: { id: string; recommended_action: string; decision_state: string; approved_refund_amount?: number };
    refund?: { id: string; provider_refund_id?: string; state: string; provider_status?: string; reconciliation_required: boolean };
  };
  derived_state: { quarantined: boolean; reconciliation_required: boolean; open_incident_count: number };
  operator_decisions: Array<{ id: string; action: string; reason_code: string; decided_at: string }>;
  incidents: Incident[];
};
type TimelineEvent = { kind: string; event_type: string; reason_code?: string; occurred_at: string; actor_type: string };
type Review = { action: string; transaction_id: string; payment_id?: string; amount: number; currency: string; fulfillment_id: string; failure_code: string; refund_on_failure: boolean; recommended_action: string; decision_state: string };
type Confirmation = { label: string; path: string; body?: Record<string, unknown>; review?: Review };
type Metrics = {
  demo_mode?: boolean; total_transactions?: number; paid_transactions?: number;
  test_gmv_minor?: number; refunds_completed?: number; unresolved_incidents?: number;
  agent_test_gmv_minor?: number; successful_agent_purchases?: number;
  agent_policy_denials?: number; agent_payment_failures_handled?: number;
  agent_fulfillment_successes?: number; agent_refund_recoveries?: number;
  average_agent_access_latency_ms?: number;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const base = configuredApiBaseEndpoint();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const response = await fetch(`${base}${path}`, { credentials: "include", cache: "no-store", headers: { Accept: "application/json", ...init?.headers }, ...init });
  if (!response.ok) throw new Error(response.status === 403 ? "This account does not have an active operator role or needs recent passkey authentication." : `Operator API returned HTTP ${response.status}`);
  return await response.json() as T;
}

function money(amount: number, currency: string) {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency }).format(amount / 100);
}

function EvidenceCard({ title, value }: { title: string; value: unknown }) {
  return <div className="rounded-lg border border-white/10 bg-black/20 p-3"><dt className="text-[10px] font-semibold uppercase tracking-[.16em] text-slate-500">{title}</dt><dd className="mt-1 break-all text-sm text-slate-200">{value === null || value === undefined ? "—" : String(value)}</dd></div>;
}

export function OperatorDashboard() {
  const { apiBaseEndpoint, establishSession, requestAuthenticated } = useAccountSession();
  const [items, setItems] = useState<WorkItem[]>([]);
  const [alerts, setAlerts] = useState<WorkItem[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [metrics, setMetrics] = useState<Metrics>({});
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [csrf, setCsrf] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reauthenticating, setReauthenticating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [work, alertRows, system, metricRows, txs, auth] = await Promise.all([
        api<WorkItem[]>("/operator/work-items"), api<WorkItem[]>("/operator/alerts"),
        api<Health>("/operator/system-health"), api<Metrics>("/operator/metrics/summary"),
        api<Transaction[]>("/operator/transactions"), api<{ csrf_token: string }>("/auth/session"),
      ]);
      setItems(work); setAlerts(alertRows); setHealth(system); setMetrics(metricRows); setTransactions(txs); setCsrf(auth.csrf_token);
    } catch (value) { setError(value instanceof Error ? value.message : "Operator data could not be loaded"); }
    finally { setLoading(false); }
  }, []);

  const openTransaction = useCallback(async (transactionId: string) => {
    setError(null);
    try {
      const [caseDetail, events] = await Promise.all([
        api<CaseDetail>(`/operator/transactions/${transactionId}/detail`),
        api<TimelineEvent[]>(`/operator/transactions/${transactionId}/timeline`),
      ]);
      setDetail(caseDetail); setTimeline(events);
    } catch (value) { setError(value instanceof Error ? value.message : "Case detail could not be loaded"); }
  }, []);

  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => window.clearTimeout(timer); }, [load]);

  async function prepareCompensation(action: "approve" | "reject") {
    const id = detail?.facts.compensation?.id; if (!id) return;
    const review = await api<Review>(`/operator/compensations/${id}/review`);
    setConfirmation({
      label: action === "approve" ? "Approve Full Refund" : "Reject Compensation",
      path: `/operator/compensations/${id}/${action}`,
      body: { reason_code: action === "approve" ? "OPERATOR_APPROVE_CONFIRMED_NON_DELIVERY" : "OPERATOR_REJECT_VALUE_ALREADY_DELIVERED", note: { source: "operator_dashboard_confirmation" } }, review,
    });
  }

  function prepareReconciliation(kind: "payment" | "refund" | "fulfillment", id: string) {
    setConfirmation({ label: `Run ${kind[0].toUpperCase()}${kind.slice(1)} Reconciliation`, path: `/operator/${kind === "payment" ? "payments" : `${kind}s`}/${id}/reconcile` });
  }

  async function submitConfirmed() {
    if (!confirmation || !csrf) return;
    setError(null);
    try {
      await api(confirmation.path, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: confirmation.body ? JSON.stringify(confirmation.body) : undefined });
      const transactionId = detail?.facts.transaction_id; setConfirmation(null); await load(); if (transactionId) await openTransaction(transactionId);
    } catch (value) { setError(value instanceof Error ? value.message : "The operator action failed safely"); }
  }

  async function reauthenticate() {
    if (!apiBaseEndpoint || reauthenticating) return;
    setError(null); setNotice(null); setReauthenticating(true);
    try {
      const options = await requestAuthenticated(`${apiBaseEndpoint}/auth/reauth/options`, { method: "POST", body: {} });
      if (typeof options.challenge_id !== "string" || typeof options.public_key !== "object" || options.public_key === null) {
        throw new Error("The authentication service returned invalid reauthentication options.");
      }
      const credential = await getPasskeyCredential(options.public_key);
      const session = await requestAuthenticated(`${apiBaseEndpoint}/auth/reauth/verify`, {
        method: "POST",
        body: { challenge_id: options.challenge_id, credential },
      });
      establishSession(session);
      if (typeof session.csrf_token === "string") setCsrf(session.csrf_token);
      setNotice("Recent passkey authentication confirmed. High-risk operator actions are available for the configured window.");
      await load();
    } catch (value) {
      setError(value instanceof Error ? value.message : "Passkey reauthentication failed safely");
    } finally { setReauthenticating(false); }
  }

  const facts = detail?.facts;
  const canDecide = facts?.compensation?.decision_state === "manual_review";
  return <main className="mx-auto min-h-screen max-w-7xl px-4 py-7 text-slate-100 sm:px-8">
    <header className="flex flex-col gap-4 border-b border-white/10 pb-7 sm:flex-row sm:items-end sm:justify-between"><div><p className="text-xs font-semibold uppercase tracking-[.22em] text-cyan-300">MeterGate operations · Test Mode</p><h1 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">Operator control plane</h1><p className="mt-2 max-w-2xl text-sm text-slate-400">Immutable facts, server-derived recovery state, and explicitly confirmed decisions.</p></div><div className="flex flex-wrap gap-2"><button disabled={reauthenticating} onClick={() => void reauthenticate()} className="rounded-lg border border-violet-300/30 bg-violet-300/10 px-4 py-2 text-sm text-violet-100 disabled:cursor-wait disabled:opacity-60">{reauthenticating ? "Complete Windows Hello…" : "Reauthenticate with Passkey"}</button><button onClick={() => void load()} className="rounded-lg border border-cyan-300/30 bg-cyan-300/10 px-4 py-2 text-sm text-cyan-100">Refresh evidence</button></div></header>
    {loading && <p className="py-10 text-slate-400">Loading authoritative operational state…</p>}
    {error && <section className="my-6 rounded-xl border border-rose-400/30 bg-rose-400/10 p-5 text-sm text-rose-100">{error}</section>}
    {notice && <section className="my-6 rounded-xl border border-emerald-400/30 bg-emerald-400/10 p-5 text-sm text-emerald-100">{notice}</section>}
    {!loading && !error && <>
      <section className="grid gap-3 py-7 sm:grid-cols-2 lg:grid-cols-5">{[["Transactions", metrics.total_transactions], ["Paid", metrics.paid_transactions], ["Test GMV", money(metrics.test_gmv_minor ?? 0, "INR")], ["Refunded", metrics.refunds_completed], ["Open incidents", metrics.unresolved_incidents]].map(([label, value]) => <article key={label} className="rounded-xl border border-white/10 bg-white/[.035] p-4"><p className="text-xs uppercase tracking-wider text-slate-500">{label}</p><p className="mt-2 text-2xl font-semibold">{value ?? 0}</p></article>)}</section>
      {metrics.demo_mode && <section className="mb-7"><h2 className="text-xl font-semibold">Hackathon agent metrics</h2><p className="mt-1 text-xs text-slate-500">Derived from MCP audit IDs and authoritative commerce records; no synthetic counters.</p><div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{[["Agent Test GMV", money(metrics.agent_test_gmv_minor ?? 0, "INR")], ["Successful agent purchases", metrics.successful_agent_purchases], ["Policy denials", metrics.agent_policy_denials], ["Payment failures handled", metrics.agent_payment_failures_handled], ["Fulfillment successes", metrics.agent_fulfillment_successes], ["Refund recoveries", metrics.agent_refund_recoveries], ["Average access latency", `${Math.round(metrics.average_agent_access_latency_ms ?? 0)} ms`]].map(([label, value]) => <article key={label} className="rounded-xl border border-cyan-300/15 p-4"><p className="text-xs uppercase tracking-wider text-slate-500">{label}</p><p className="mt-2 text-xl font-semibold">{value ?? 0}</p></article>)}</div></section>}
      <section className="grid gap-6 lg:grid-cols-[1.2fr_.8fr]"><div><h2 className="text-xl font-semibold">Recovery queue</h2><div className="mt-3 overflow-hidden rounded-xl border border-white/10">{items.length === 0 ? <p className="p-5 text-sm text-slate-400">No unresolved projected work.</p> : items.map(item => <button key={`${item.type}:${item.resource_id}`} onClick={() => item.transaction_id && void openTransaction(item.transaction_id)} disabled={!item.transaction_id} className="grid w-full gap-1 border-t border-white/[.07] px-4 py-3 text-left text-sm first:border-t-0 enabled:hover:bg-white/[.04] sm:grid-cols-[1.5fr_.6fr_1fr]"><strong>{item.type.replaceAll("_", " ")}</strong><span className={item.severity === "critical" ? "text-rose-300" : "text-amber-300"}>{item.severity}</span><code className="truncate text-xs text-cyan-200">{item.resource_id}</code><span className="text-xs text-slate-500 sm:col-span-3">{item.reason_code} · {Math.floor(item.age_seconds / 60)}m</span></button>)}</div></div>
      <div><h2 className="text-xl font-semibold">Alerts</h2><div className="mt-3 space-y-2">{alerts.length === 0 ? <p className="rounded-xl border border-white/10 p-4 text-sm text-slate-400">No active high-severity alerts.</p> : alerts.map(alert => <div key={`${alert.type}:${alert.resource_id}`} className="rounded-lg border border-amber-300/20 bg-amber-300/[.06] p-3 text-sm"><strong>{alert.type.replaceAll("_", " ")}</strong><p className="mt-1 text-xs text-amber-100/60">{alert.reason_code}</p></div>)}</div></div></section>
      <section className="mt-8"><h2 className="text-xl font-semibold">Historical commerce</h2><div className="mt-3 flex gap-3 overflow-x-auto pb-2">{transactions.map(tx => <button key={tx.transaction_id} onClick={() => void openTransaction(tx.transaction_id)} className="min-w-72 rounded-xl border border-white/10 p-4 text-left hover:border-cyan-300/30"><code className="text-xs text-cyan-200">{tx.transaction_id}</code><p className="mt-2 font-medium">{money(tx.amount, tx.currency)} · {tx.payment_state}</p><p className="mt-1 text-xs text-slate-500">{tx.account_id}</p></button>)}</div></section>
      {detail && facts && <section className="mt-8 rounded-2xl border border-cyan-300/20 bg-cyan-950/[.12] p-4 sm:p-6"><div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between"><div><p className="text-xs uppercase tracking-[.2em] text-cyan-300">Case detail</p><h2 className="mt-2 text-xl font-semibold">{facts.transaction_id}</h2></div><span className="text-sm text-slate-400">{money(facts.amount, facts.currency)} · {facts.payment_state}</span></div>
        <div className="mt-6 grid gap-5 lg:grid-cols-3"><div><h3 className="mb-3 text-xs font-semibold uppercase tracking-[.18em] text-emerald-300">Facts</h3><dl className="grid gap-2"><EvidenceCard title="Account" value={facts.account_id}/><EvidenceCard title="Merchant / service" value={`${facts.merchant_name ?? "—"} / ${facts.service_name ?? "—"}`}/><EvidenceCard title="Razorpay order" value={facts.provider_order_id}/><EvidenceCard title="Captured payment" value={facts.provider_payment_id}/><EvidenceCard title="Provider status" value={facts.provider_payment_status}/><EvidenceCard title="Fulfillment" value={facts.fulfillment ? `${facts.fulfillment.id} · ${facts.fulfillment.state} · ${facts.fulfillment.attempt_count} attempts · ${facts.fulfillment.failure_code ?? "no failure"}` : undefined}/><EvidenceCard title="Refund" value={facts.refund ? `${facts.refund.id} · ${facts.refund.state} · ${facts.refund.provider_refund_id ?? "unbound"} · provider ${facts.refund.provider_status ?? "unknown"}` : undefined}/></dl></div>
        <div><h3 className="mb-3 text-xs font-semibold uppercase tracking-[.18em] text-amber-300">Derived state</h3><dl className="grid gap-2"><EvidenceCard title="Quarantined" value={detail.derived_state.quarantined}/><EvidenceCard title="Reconciliation required" value={detail.derived_state.reconciliation_required}/><EvidenceCard title="Open incidents" value={detail.derived_state.open_incident_count}/><EvidenceCard title="Compensation policy" value={facts.compensation?.recommended_action}/><EvidenceCard title="Decision state" value={facts.compensation?.decision_state}/><EvidenceCard title="Refund overlay" value={facts.refund?.reconciliation_required}/></dl></div>
        <div><h3 className="mb-3 text-xs font-semibold uppercase tracking-[.18em] text-violet-300">Operator decision</h3><div className="space-y-2">{detail.operator_decisions.length ? detail.operator_decisions.map(decision => <div key={decision.id} className="rounded-lg border border-violet-300/15 p-3 text-sm"><strong>{decision.action}</strong><p className="mt-1 text-xs text-slate-500">{decision.reason_code}</p></div>) : <p className="text-sm text-slate-500">No operator decision recorded.</p>}</div><div className="mt-4 grid gap-2">{canDecide && <><button onClick={() => void prepareCompensation("approve")} className="rounded-lg bg-emerald-400 px-3 py-2 text-sm font-semibold text-emerald-950">Approve Full Refund</button><button onClick={() => void prepareCompensation("reject")} className="rounded-lg border border-rose-300/30 px-3 py-2 text-sm text-rose-200">Reject Compensation</button></>}{["order_creation_pending", "order_created", "order_creation_uncertain", "payment_pending", "payment_authorized", "reconciliation_required"].includes(facts.payment_state) && <button onClick={() => prepareReconciliation("payment", facts.transaction_id)} className="rounded-lg border border-cyan-300/30 px-3 py-2 text-sm text-cyan-200">Run Payment Reconciliation</button>}{facts.refund?.reconciliation_required && <button onClick={() => prepareReconciliation("refund", facts.refund!.id)} className="rounded-lg border border-cyan-300/30 px-3 py-2 text-sm text-cyan-200">Run Refund Reconciliation</button>}{facts.fulfillment?.state === "reconciliation_required" && <button onClick={() => prepareReconciliation("fulfillment", facts.fulfillment!.id)} className="rounded-lg border border-cyan-300/30 px-3 py-2 text-sm text-cyan-200">Run Fulfillment Reconciliation</button>}</div></div></div>
        <h3 className="mt-7 text-xs font-semibold uppercase tracking-[.18em] text-slate-400">Unified timeline</h3><ol className="mt-3 border-l border-white/10 pl-4">{timeline.map((event, index) => <li key={`${event.occurred_at}:${event.kind}:${index}`} className="relative pb-4 text-sm before:absolute before:-left-[1.28rem] before:top-1.5 before:h-2 before:w-2 before:rounded-full before:bg-cyan-300"><strong>{event.event_type.replaceAll("_", " ").toUpperCase()}</strong><p className="text-xs text-slate-500">{new Date(event.occurred_at).toLocaleString()} · {event.kind} · {event.reason_code ?? "recorded evidence"}</p></li>)}</ol>
      </section>}
      <section className="mt-8"><h2 className="text-xl font-semibold">Worker & outbox health</h2><div className="mt-3 grid gap-3 sm:grid-cols-3">{health?.workers.map(worker => <article key={worker.worker_type} className={`rounded-xl border p-4 ${worker.status === "healthy" ? "border-emerald-300/20" : "border-rose-300/30 bg-rose-300/[.05]"}`}><div className="flex justify-between"><strong>{worker.worker_type}</strong><span className={worker.status === "healthy" ? "text-emerald-300" : "text-rose-300"}>{worker.status}</span></div><p className="mt-2 text-xs text-slate-500">Heartbeat {worker.last_heartbeat ? new Date(worker.last_heartbeat).toLocaleString() : "never"}</p><p className="mt-1 text-xs text-slate-500">Successful work {worker.last_successful_work ? new Date(worker.last_successful_work).toLocaleString() : "not recorded"}</p><p className="mt-1 text-xs text-slate-500">Backlog {worker.backlog_count}</p></article>)}</div><div className="mt-3 flex flex-wrap gap-3 text-xs text-slate-400">{Object.entries(health?.queues ?? {}).map(([key, value]) => <span key={key} className="rounded-full border border-white/10 px-3 py-1">{key.replaceAll("_", " ")}: {value}</span>)}</div></section>
    </>}
    {confirmation && <div className="fixed inset-0 z-50 grid place-items-center bg-black/75 p-4"><section role="dialog" aria-modal="true" aria-labelledby="confirm-title" className="w-full max-w-lg rounded-2xl border border-cyan-300/25 bg-[#10151d] p-6 shadow-2xl"><p className="text-xs uppercase tracking-[.2em] text-amber-300">Explicit confirmation required</p><h2 id="confirm-title" className="mt-2 text-2xl font-semibold">{confirmation.label}</h2>{confirmation.review && <dl className="mt-5 grid grid-cols-2 gap-2"><EvidenceCard title="Transaction" value={confirmation.review.transaction_id}/><EvidenceCard title="Payment" value={confirmation.review.payment_id}/><EvidenceCard title="Server-derived amount" value={money(confirmation.review.amount, confirmation.review.currency)}/><EvidenceCard title="Fulfillment" value={confirmation.review.fulfillment_id}/><EvidenceCard title="Failure code" value={confirmation.review.failure_code}/><EvidenceCard title="Refund on failure" value={confirmation.review.refund_on_failure}/><EvidenceCard title="Recommended action" value={confirmation.review.recommended_action}/><EvidenceCard title="Decision state" value={confirmation.review.decision_state}/></dl>}<p className="mt-5 text-sm text-slate-400">This uses current server evidence and cannot directly edit provider or commerce state.</p><div className="mt-6 flex justify-end gap-3"><button onClick={() => setConfirmation(null)} className="rounded-lg border border-white/15 px-4 py-2 text-sm">Cancel</button><button onClick={() => void submitConfirmed()} className="rounded-lg bg-cyan-300 px-4 py-2 text-sm font-semibold text-cyan-950">Confirm action</button></div></section></div>}
  </main>;
}
