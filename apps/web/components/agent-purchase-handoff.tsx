"use client";

import { useEffect, useState } from "react";

import { useAccountSession } from "@/components/account-session";
import { TrustedApproval } from "@/components/trusted-approval";
import { ApiRequestFailure } from "@/lib/api-client";

type Handoff = {
  policySubject: string;
  decision: string;
  reasonCodes: string[];
  amount: number;
  currency: string;
  merchant: string;
  service: string;
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
        if (!active) return;
        setHandoff({
          policySubject: requiredString(policy, "subject_ref"),
          decision: requiredString(evaluation, "decision"),
          reasonCodes: Array.isArray(evaluation.reason_codes)
            ? evaluation.reason_codes.filter((item): item is string => typeof item === "string")
            : [],
          amount: requiredNumber(pricing, "amount"),
          currency: requiredString(pricing, "currency"),
          merchant: requiredString(merchant, "name"),
          service: requiredString(service, "name"),
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
  if (handoff.decision !== "allow") {
    return <p role="alert" className="mt-5 rounded-xl border border-rose-300/20 p-4 text-sm text-rose-100">Policy denied this quote: {handoff.reasonCodes.join(", ")}</p>;
  }
  return (
    <div className="mt-6">
      <dl className="grid gap-3 rounded-2xl border border-white/10 bg-black/20 p-4 text-sm sm:grid-cols-2">
        <div><dt className="text-slate-500">Merchant</dt><dd className="mt-1 text-white">{handoff.merchant}</dd></div>
        <div><dt className="text-slate-500">Service</dt><dd className="mt-1 text-white">{handoff.service}</dd></div>
        <div><dt className="text-slate-500">Server price</dt><dd className="mt-1 text-white">{handoff.currency} {(handoff.amount / 100).toFixed(2)}</dd></div>
        <div><dt className="text-slate-500">Policy</dt><dd className="mt-1 font-mono text-emerald-200">ALLOW</dd></div>
      </dl>
      <TrustedApproval apiBaseEndpoint={apiBaseEndpoint} evaluationId={evaluationId} policySubjectRef={handoff.policySubject} />
    </div>
  );
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
