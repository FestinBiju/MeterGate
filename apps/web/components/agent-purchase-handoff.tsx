"use client";

import { useEffect, useState } from "react";

import { useAccountSession } from "@/components/account-session";
import { TrustedApproval } from "@/components/trusted-approval";
import { ApiRequestFailure } from "@/lib/api-client";

type PolicyCheck = {
  rule: string;
  result: "pass" | "fail";
  reasonCode: string;
  details: Record<string, unknown>;
};

type Handoff = {
  policySubject: string;
  decision: string;
  reasonCodes: string[];
  amount: number;
  maximumAmount: number;
  currency: string;
  purchaseType: string;
  allowedCurrencies: string[];
  allowedPurchaseTypes: string[];
  merchant: string;
  service: string;
  quoteId: string;
  input: unknown;
  checks: PolicyCheck[];
};

export function AgentPurchaseHandoff({ evaluationId }: { evaluationId: string }) {
  const { apiBaseEndpoint, state, requestAuthenticated } = useAccountSession();
  const [handoff, setHandoff] = useState<Handoff | null>(null);
  const [failure, setFailure] = useState<{ code: string; message: string } | null>(null);

  useEffect(() => {
    if (!apiBaseEndpoint || state.kind !== "authenticated") return;
    let active = true;
    const load = async () => {
      try {
        const evaluation = await requestAuthenticated(
          `${apiBaseEndpoint}/policy-evaluations/${encodeURIComponent(evaluationId)}`,
          { method: "GET" },
        );
        const policyId = requiredString(evaluation, "policy_id");
        const quoteId = requiredString(evaluation, "quote_id");
        const [policy, quote] = await Promise.all([
          requestAuthenticated(`${apiBaseEndpoint}/policies/${encodeURIComponent(policyId)}`, { method: "GET" }),
          requestAuthenticated(`${apiBaseEndpoint}/quotes/${encodeURIComponent(quoteId)}`, { method: "GET" }),
        ]);
        const pricing = requiredRecord(quote, "pricing");
        const merchant = requiredRecord(quote, "merchant");
        const service = requiredRecord(quote, "service");
        const constraints = requiredRecord(policy, "constraints");
        if (!active) return;
        setHandoff({
          policySubject: requiredString(policy, "subject_ref"),
          decision: requiredString(evaluation, "decision"),
          reasonCodes: stringArray(evaluation.reason_codes),
          amount: requiredNumber(pricing, "amount"),
          maximumAmount: requiredNumber(constraints, "maximum_amount"),
          currency: requiredString(pricing, "currency"),
          purchaseType: requiredString(pricing, "purchase_type"),
          allowedCurrencies: stringArray(constraints.allowed_currencies),
          allowedPurchaseTypes: stringArray(constraints.allowed_purchase_types),
          merchant: requiredString(merchant, "name"),
          service: requiredString(service, "name"),
          quoteId,
          input: quote.input,
          checks: policyChecks(evaluation.checks),
        });
        setFailure(null);
      } catch (error) {
        if (!active) return;
        setFailure(
          error instanceof ApiRequestFailure
            ? { code: error.code, message: error.message }
            : { code: "AGENT_HANDOFF_INVALID", message: "The agent purchase handoff could not be verified." },
        );
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [apiBaseEndpoint, evaluationId, requestAuthenticated, state.kind]);

  if (state.kind !== "authenticated") {
    return <p className="mt-5 text-sm text-slate-400">Sign in with the owning passkey to inspect this agent handoff.</p>;
  }
  if (failure) {
    return <p role="alert" className="mt-5 rounded-xl border border-rose-300/20 p-4 text-sm text-rose-100"><span className="font-mono">{failure.code}</span> · {failure.message}</p>;
  }
  if (!handoff || !apiBaseEndpoint) {
    return <p role="status" className="mt-5 text-sm text-slate-400">Loading server-authoritative purchase evidence…</p>;
  }

  const allowed = handoff.decision === "allow";
  return (
    <div className="mt-6 space-y-5">
      <section aria-labelledby="agent-proposal-heading" className="rounded-2xl border border-white/10 p-5">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-violet-300">AI proposes</p>
        <h2 id="agent-proposal-heading" className="mt-2 text-xl font-semibold text-white">Agent proposal</h2>
        <p className="mt-2 text-sm leading-6 text-slate-400">Codex selected a catalog service. These are the immutable server records attached to that proposal, not model-authored commercial terms.</p>
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
          <Evidence label="Merchant" value={handoff.merchant} />
          <Evidence label="Service" value={handoff.service} />
          <Evidence label="Normalized input" value={formatValue(handoff.input)} mono />
          <Evidence label="Immutable quote" value={handoff.quoteId} mono />
        </dl>
      </section>

      <section aria-labelledby="deterministic-checks-heading" className="rounded-2xl border border-white/10 p-5">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-cyan-300">Code authorizes</p>
        <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 id="deterministic-checks-heading" className="text-xl font-semibold text-white">Deterministic checks</h2>
            <p className="mt-2 text-sm leading-6 text-slate-400">The model cannot change these quote, policy, or reason-coded evaluation facts.</p>
          </div>
          <span className={`font-mono text-sm font-semibold ${allowed ? "text-emerald-200" : "text-rose-200"}`}>{handoff.decision.toUpperCase()}</span>
        </div>
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <Evidence label="Server price" value={money(handoff.amount, handoff.currency)} />
          <Evidence label="Buyer maximum" value={money(handoff.maximumAmount, handoff.currency)} />
          <Evidence label="Allowed currency" value={handoff.allowedCurrencies.join(", ") || "Unconstrained"} />
          <Evidence label="Purchase type" value={`${handoff.purchaseType} · ${handoff.allowedPurchaseTypes.join(", ") || "unconstrained"}`} />
        </dl>
        <ol className="mt-5 divide-y divide-white/10 border-y border-white/10" aria-label="Policy evaluation checks">
          {handoff.checks.map((check) => (
            <li key={check.rule} className="grid gap-2 py-3 text-sm sm:grid-cols-[minmax(0,.65fr)_minmax(0,1fr)] sm:items-center">
              <div className="flex min-w-0 items-center gap-3">
                <span className={`inline-flex min-w-12 justify-center rounded-full border px-2 py-1 font-mono text-[10px] font-semibold uppercase ${check.result === "pass" ? "border-emerald-300/30 text-emerald-200" : "border-rose-300/30 text-rose-200"}`}>{check.result}</span>
                <strong className="break-words font-mono text-xs text-white">{check.rule}</strong>
              </div>
              <div className="min-w-0 sm:text-right">
                <p className={check.result === "pass" ? "text-emerald-200" : "text-rose-200"}>{check.reasonCode}</p>
                {Object.keys(check.details).length > 0 && <p className="mt-1 break-words font-mono text-[11px] text-slate-500">{formatValue(check.details)}</p>}
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="human-authority-heading" className="rounded-2xl border border-white/10 p-5">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-amber-300">Human decides</p>
        <h2 id="human-authority-heading" className="mt-2 text-xl font-semibold text-white">Human authority</h2>
        {allowed ? (
          <>
            <p className="mt-2 text-sm leading-6 text-slate-400">Policy allowance is not purchase authorization. Review the exact evidence, approve with the owning passkey, then complete Razorpay Test Mode Checkout.</p>
            <TrustedApproval apiBaseEndpoint={apiBaseEndpoint} evaluationId={evaluationId} policySubjectRef={handoff.policySubject} />
          </>
        ) : (
          <p role="alert" className="mt-4 rounded-xl border border-rose-300/20 bg-rose-400/10 p-4 text-sm text-rose-100">Policy denied this quote: <span className="font-mono">{handoff.reasonCodes.join(", ")}</span>. No approval or payment path was created.</p>
        )}
      </section>
    </div>
  );
}

function Evidence({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div><dt className="text-xs text-slate-500">{label}</dt><dd className={`mt-1 break-words text-white ${mono ? "font-mono text-xs" : ""}`}>{value}</dd></div>;
}

function money(amount: number, currency: string): string {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency }).format(amount / 100);
}

function formatValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function requiredRecord(record: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = record[key];
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error(key);
  return value as Record<string, unknown>;
}

function requiredString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) throw new Error(key);
  return value;
}

function requiredNumber(record: Record<string, unknown>, key: string): number {
  const value = record[key];
  if (typeof value !== "number" || !Number.isSafeInteger(value)) throw new Error(key);
  return value;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function policyChecks(value: unknown): PolicyCheck[] {
  if (!Array.isArray(value)) throw new Error("checks");
  return value.map((item) => {
    if (typeof item !== "object" || item === null || Array.isArray(item)) throw new Error("checks");
    const record = item as Record<string, unknown>;
    const result = requiredString(record, "result");
    if (result !== "pass" && result !== "fail") throw new Error("checks.result");
    return {
      rule: requiredString(record, "rule"),
      result,
      reasonCode: requiredString(record, "reason_code"),
      details: requiredRecord(record, "details"),
    };
  });
}
