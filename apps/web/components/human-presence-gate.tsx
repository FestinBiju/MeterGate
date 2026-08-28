"use client";

import { useState } from "react";

import { useAccountSession } from "@/components/account-session";
import { ApiRequestFailure } from "@/lib/api-client";
import { getPasskeyCredential, WebAuthnBrowserFailure } from "@/lib/webauthn";

type Props = {
  resourceBinding: { scopes: string[]; expires_in_seconds: number };
  buttonLabel?: string;
  disabled?: boolean;
  onVerified: (proofId: string) => void | Promise<void>;
};

export function HumanPresenceGate({ resourceBinding, buttonLabel = "Verify human presence", disabled, onVerified }: Props) {
  const { apiBaseEndpoint, requestAuthenticated } = useAccountSession();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const verify = async () => {
    if (!apiBaseEndpoint || busy || disabled) return;
    setBusy(true);
    setFailure(null);
    try {
      const challenge = await requestAuthenticated(`${apiBaseEndpoint}/human-presence/challenge`, {
        method: "POST",
        body: { action_class: "new_agent_session", resource_binding: resourceBinding },
      });
      if (
        typeof challenge.challenge_id !== "string" ||
        typeof challenge.public_key !== "object" ||
        challenge.public_key === null ||
        (challenge.public_key as Record<string, unknown>).userVerification !== "required"
      ) {
        throw new Error("Human-presence challenge was invalid");
      }
      const credential = await getPasskeyCredential(challenge.public_key);
      const proof = await requestAuthenticated(
        `${apiBaseEndpoint}/human-presence/${encodeURIComponent(challenge.challenge_id)}/verify`,
        { method: "POST", body: { credential } },
      );
      if (typeof proof.id !== "string" || !proof.id.startsWith("hpp_")) {
        throw new Error("Human-presence proof was invalid");
      }
      await onVerified(proof.id);
    } catch (error) {
      setFailure(
        error instanceof ApiRequestFailure || error instanceof WebAuthnBrowserFailure
          ? error.message
          : "Human presence could not be verified safely.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 rounded-xl border border-emerald-300/20 bg-emerald-300/[0.05] p-3">
      <p className="text-xs font-semibold text-emerald-100">Human verification required</p>
      <p className="mt-1 text-xs leading-5 text-slate-300">
        MeterGate does not use image CAPTCHAs. Sensitive actions require a short-lived,
        action-bound passkey verification.
      </p>
      <button
        type="button"
        disabled={busy || disabled}
        onClick={() => void verify()}
        className="mt-3 rounded-lg border border-emerald-200/25 px-3 py-2 text-xs font-semibold text-emerald-100 disabled:opacity-50"
      >
        {busy ? "Waiting for passkey verification…" : buttonLabel}
      </button>
      <details className="mt-3 text-xs text-slate-400">
        <summary className="cursor-pointer text-slate-300">Why no CAPTCHA?</summary>
        <p className="mt-2 leading-5">
          Visual puzzles can increasingly be solved by AI. MeterGate instead requires control
          of the registered authenticator and explicit user verification. This raises the cost
          of automation without claiming that any mechanism is impossible to bypass.
        </p>
      </details>
      {failure ? <p role="alert" className="mt-2 text-xs text-rose-100">{failure}</p> : null}
    </div>
  );
}
