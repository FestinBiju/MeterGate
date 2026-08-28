"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";

import { useAccountSession } from "@/components/account-session";
import type { AccountFailure } from "@/components/account-session";
import {
  ApiRequestFailure,
  requestCredentialedJson,
} from "@/lib/api-client";
import {
  createPasskeyCredential,
  getPasskeyCredential,
  passkeySupportError,
  WebAuthnBrowserFailure,
} from "@/lib/webauthn";

type CeremonyOptions = {
  challenge_id: string;
  public_key: Record<string, unknown>;
  expires_at: string;
};

type AuthPhase =
  | "idle"
  | "signup-options"
  | "signup-browser"
  | "signup-verifying"
  | "login-options"
  | "login-browser"
  | "login-verifying"
  | "logout";

const dateTimeFormatter = new Intl.DateTimeFormat("en-IN", {
  dateStyle: "medium",
  timeStyle: "medium",
});

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isTimestamp(value: unknown): value is string {
  return isNonEmptyString(value) && Number.isFinite(Date.parse(value));
}

function parseCeremonyOptions(value: unknown): CeremonyOptions | null {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.challenge_id) ||
    !isRecord(value.public_key) ||
    !isTimestamp(value.expires_at)
  ) {
    return null;
  }

  return {
    challenge_id: value.challenge_id,
    public_key: value.public_key,
    expires_at: value.expires_at,
  };
}

function authFailure(
  error: unknown,
  operation: "signup" | "login" | "logout",
): AccountFailure {
  if (error instanceof ApiRequestFailure) {
    return { code: error.code, message: error.message };
  }

  if (error instanceof WebAuthnBrowserFailure) {
    const code =
      error.kind === "cancelled"
        ? operation === "signup"
          ? "AUTH_SIGNUP_CANCELLED"
          : "AUTH_LOGIN_CANCELLED"
        : error.kind === "unsupported"
          ? "AUTH_PASSKEY_UNSUPPORTED"
          : operation === "signup"
            ? "AUTH_SIGNUP_VERIFICATION_FAILED"
            : "AUTH_LOGIN_VERIFICATION_FAILED";
    return { code, message: error.message };
  }

  return {
    code: `AUTH_${operation.toUpperCase()}_FAILED`,
    message: `The ${operation} operation could not be completed.`,
  };
}

function FailureNotice({ failure }: { failure: AccountFailure }) {
  return (
    <div
      role="alert"
      className="mt-4 rounded-xl border border-rose-300/15 bg-rose-300/[0.05] px-4 py-3"
    >
      <p className="font-mono text-[10px] text-rose-200/80">{failure.code}</p>
      <p className="mt-1 text-xs leading-5 text-rose-100/85">
        {failure.message}
      </p>
    </div>
  );
}

function phaseMessage(phase: AuthPhase): string | null {
  switch (phase) {
    case "signup-options":
      return "Preparing secure account registration…";
    case "signup-browser":
      return "Complete the passkey registration prompt on this device.";
    case "signup-verifying":
      return "Verifying the first passkey and activating the account…";
    case "login-options":
      return "Preparing passkey sign-in…";
    case "login-browser":
      return "Choose and verify your MeterGate passkey.";
    case "login-verifying":
      return "Verifying the passkey and creating a server-side session…";
    case "logout":
      return "Revoking the active server-side session…";
    default:
      return null;
  }
}

export function AccountAuth() {
  const {
    apiBaseEndpoint,
    state,
    establishSession,
    clearSession,
    refreshSession,
    requestAuthenticated,
  } = useAccountSession();
  const displayNameId = useId();
  const displayNameErrorId = useId();
  const authenticatedHeadingRef = useRef<HTMLHeadingElement>(null);
  const anonymousHeadingRef = useRef<HTMLHeadingElement>(null);
  const priorStateKind = useRef(state.kind);
  const [displayName, setDisplayName] = useState("");
  const [displayNameError, setDisplayNameError] = useState<string | null>(null);
  const [phase, setPhase] = useState<AuthPhase>("idle");
  const [failure, setFailure] = useState<AccountFailure | null>(null);
  const [supportChecked, setSupportChecked] = useState(false);
  const [supportFailure, setSupportFailure] = useState<AccountFailure | null>(
    null,
  );

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      const message = passkeySupportError();
      setSupportChecked(true);
      setSupportFailure(
        message ? { code: "AUTH_PASSKEY_UNSUPPORTED", message } : null,
      );
    }, 0);

    return () => window.clearTimeout(timeout);
  }, []);

  useEffect(() => {
    const previousKind = priorStateKind.current;
    priorStateKind.current = state.kind;
    if (previousKind === "checking" || previousKind === state.kind) {
      return;
    }

    const timeout = window.setTimeout(() => {
      if (state.kind === "authenticated") {
        authenticatedHeadingRef.current?.focus();
      } else if (state.kind === "anonymous") {
        anonymousHeadingRef.current?.focus();
      }
    }, 0);

    return () => window.clearTimeout(timeout);
  }, [state.kind]);

  const busy = phase !== "idle";
  const canUsePasskeys =
    supportChecked && supportFailure === null && apiBaseEndpoint !== null;

  const finishAuthentication = (body: Record<string, unknown>) => {
    establishSession(body);
    setFailure(null);
    setPhase("idle");
  };

  const recoverTimedOutVerification = async (
    error: unknown,
  ): Promise<boolean> => {
    if (
      !(error instanceof ApiRequestFailure) ||
      error.code !== "API_REQUEST_TIMEOUT"
    ) {
      return false;
    }

    try {
      return (await refreshSession()) !== null;
    } catch {
      return false;
    }
  };

  const signup = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedName = displayName.trim();
    if (!normalizedName) {
      setDisplayNameError("Enter a display name for this MeterGate account.");
      return;
    }
    if (!apiBaseEndpoint || !canUsePasskeys || busy) {
      return;
    }

    setDisplayNameError(null);
    setFailure(null);
    setPhase("signup-options");
    try {
      const optionsBody = await requestCredentialedJson(
        `${apiBaseEndpoint}/auth/signup/options`,
        { method: "POST", body: { display_name: normalizedName } },
      );
      const options = parseCeremonyOptions(optionsBody);
      if (!options) {
        throw new ApiRequestFailure(
          "AUTH_SIGNUP_RESPONSE_INVALID",
          "The authentication service returned unexpected signup options.",
        );
      }

      setPhase("signup-browser");
      const credential = await createPasskeyCredential(options.public_key);
      setPhase("signup-verifying");

      try {
        const sessionBody = await requestCredentialedJson(
          `${apiBaseEndpoint}/auth/signup/verify`,
          {
            method: "POST",
            body: { challenge_id: options.challenge_id, credential },
          },
        );
        finishAuthentication(sessionBody);
      } catch (error: unknown) {
        if (await recoverTimedOutVerification(error)) {
          setFailure(null);
          setPhase("idle");
          return;
        }
        throw error;
      }
    } catch (error: unknown) {
      setPhase("idle");
      setFailure(authFailure(error, "signup"));
    }
  };

  const login = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!apiBaseEndpoint || !canUsePasskeys || busy) {
      return;
    }

    setFailure(null);
    setPhase("login-options");
    try {
      const optionsBody = await requestCredentialedJson(
        `${apiBaseEndpoint}/auth/login/options`,
        { method: "POST", body: {} },
      );
      const options = parseCeremonyOptions(optionsBody);
      if (!options) {
        throw new ApiRequestFailure(
          "AUTH_LOGIN_RESPONSE_INVALID",
          "The authentication service returned unexpected login options.",
        );
      }

      setPhase("login-browser");
      const credential = await getPasskeyCredential(options.public_key);
      setPhase("login-verifying");

      try {
        const sessionBody = await requestCredentialedJson(
          `${apiBaseEndpoint}/auth/login/verify`,
          {
            method: "POST",
            body: { challenge_id: options.challenge_id, credential },
          },
        );
        finishAuthentication(sessionBody);
      } catch (error: unknown) {
        if (await recoverTimedOutVerification(error)) {
          setFailure(null);
          setPhase("idle");
          return;
        }
        throw error;
      }
    } catch (error: unknown) {
      setPhase("idle");
      setFailure(authFailure(error, "login"));
    }
  };

  const logout = async () => {
    if (!apiBaseEndpoint || state.kind !== "authenticated" || busy) {
      return;
    }

    setFailure(null);
    setPhase("logout");
    try {
      await requestAuthenticated(`${apiBaseEndpoint}/auth/logout`, {
        method: "POST",
        body: {},
        allowEmptyResponse: true,
      });
      clearSession();
      setPhase("idle");
    } catch (error: unknown) {
      setPhase("idle");
      if (error instanceof ApiRequestFailure && error.status === 401) {
        return;
      }
      setFailure(authFailure(error, "logout"));
    }
  };

  const currentPhaseMessage = phaseMessage(phase);

  return (
    <section
      id="buyer-account"
      tabIndex={-1}
      aria-labelledby="buyer-account-title"
      className="mt-20 rounded-3xl border border-white/[0.08] bg-white/[0.018] p-5 focus:outline-none sm:p-7"
    >
      <div className="max-w-2xl">
        <p className="text-xs font-semibold uppercase tracking-[0.22em] text-cyan-300">
          Authenticated buyer boundary
        </p>
        <h2
          id="buyer-account-title"
          className="mt-3 text-2xl font-semibold tracking-[-0.03em] text-white sm:text-3xl"
        >
          Buyer account
        </h2>
        <p className="mt-3 text-sm leading-6 text-slate-400">
          A MeterGate account proves control of registered passkeys. It does not
          establish legal identity or KYC.
        </p>
      </div>

      {state.kind === "checking" ? (
        <div
          role="status"
          className="mt-6 rounded-2xl border border-white/[0.08] bg-black/15 px-4 py-5 text-sm text-slate-300"
        >
          Checking for an authenticated account session…
        </div>
      ) : null}

      {state.kind === "authenticated" ? (
        <div
          aria-busy={phase === "logout"}
          className="mt-6 rounded-2xl border border-emerald-300/15 bg-emerald-300/[0.04] p-4 sm:p-5"
        >
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-emerald-200/75">
                Signed in
              </p>
              <h3
                ref={authenticatedHeadingRef}
                tabIndex={-1}
                className="mt-2 text-lg font-semibold text-white focus:outline-none"
              >
                {state.session.account.display_name}
              </h3>
              <p className="mt-1 break-all font-mono text-[11px] text-slate-400">
                {state.session.account.id}
              </p>
            </div>
            <span className="w-fit rounded-full border border-emerald-300/20 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-emerald-100">
              Active
            </span>
          </div>

          <dl className="mt-5 grid gap-3 border-t border-white/[0.07] pt-4 text-xs sm:grid-cols-3">
            <div>
              <dt className="text-slate-500">Authentication method</dt>
              <dd className="mt-1 font-medium text-slate-200">Passkey</dd>
            </div>
            <div>
              <dt className="text-slate-500">Registered passkeys</dt>
              <dd className="mt-1 text-slate-200">
                {state.session.approval_identity.credential_count}
              </dd>
            </div>
            <div>
              <dt className="text-slate-500">Session expires</dt>
              <dd className="mt-1 text-slate-200">
                <time
                  dateTime={state.session.expires_at}
                  title={state.session.expires_at}
                >
                  {dateTimeFormatter.format(new Date(state.session.expires_at))}
                </time>
              </dd>
            </div>
          </dl>

          <button
            type="button"
            disabled={busy}
            onClick={logout}
            className="mt-5 rounded-xl border border-white/10 bg-white/[0.04] px-4 py-2.5 text-sm font-medium text-slate-200 transition hover:border-rose-300/25 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-200 disabled:cursor-wait disabled:opacity-55"
          >
            {phase === "logout" ? "Signing out…" : "Sign out"}
          </button>
        </div>
      ) : null}

      {state.kind === "anonymous" ? (
        <div className="mt-6">
          <h3
            ref={anonymousHeadingRef}
            tabIndex={-1}
            className="text-base font-semibold text-white focus:outline-none"
          >
            Sign in to create buyer-owned policies and approvals
          </h3>
          <p className="mt-2 text-xs leading-5 text-slate-400">
            Catalog discovery and quote requests remain public. Buyer authority
            begins when an authenticated account creates a policy.
          </p>

          {state.failure ? <FailureNotice failure={state.failure} /> : null}
          {supportFailure ? <FailureNotice failure={supportFailure} /> : null}

          <div className="mt-5 grid gap-4 lg:grid-cols-2">
            <form
              onSubmit={signup}
              aria-busy={phase.startsWith("signup")}
              className="rounded-2xl border border-white/[0.08] bg-black/15 p-4"
            >
              <fieldset disabled={busy || !canUsePasskeys}>
                <legend className="text-sm font-semibold text-slate-100">
                  Create Account
                </legend>
                <p className="mt-2 text-[11px] leading-5 text-slate-500">
                  Your first passkey activates the account and signs this browser
                  in. No password or email is collected.
                </p>
                <label
                  htmlFor={displayNameId}
                  className="mt-4 block text-xs font-medium text-slate-300"
                >
                  Display name
                </label>
                <input
                  id={displayNameId}
                  value={displayName}
                  required
                  maxLength={200}
                  autoComplete="name"
                  aria-invalid={displayNameError ? true : undefined}
                  aria-describedby={
                    displayNameError ? displayNameErrorId : undefined
                  }
                  onChange={(event) => {
                    setDisplayName(event.target.value);
                    setDisplayNameError(null);
                    setFailure(null);
                  }}
                  className="mt-2 w-full rounded-xl border border-white/10 bg-black/25 px-3 py-2.5 text-sm text-slate-200 outline-none transition focus:border-cyan-300/35 focus:ring-2 focus:ring-cyan-300/10 disabled:cursor-wait disabled:opacity-55"
                />
                {displayNameError ? (
                  <p
                    id={displayNameErrorId}
                    role="alert"
                    className="mt-2 text-xs text-rose-200"
                  >
                    {displayNameError}
                  </p>
                ) : null}
                <button
                  type="submit"
                  className="mt-4 w-full rounded-xl border border-cyan-300/20 bg-cyan-300/[0.07] px-4 py-2.5 text-sm font-medium text-cyan-100 transition hover:border-cyan-300/35 hover:bg-cyan-300/[0.11] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-wait disabled:opacity-50"
                >
                  {phase.startsWith("signup")
                    ? "Creating account…"
                    : "Create Account with Passkey"}
                </button>
              </fieldset>
            </form>

            <form
              onSubmit={login}
              aria-busy={phase.startsWith("login")}
              className="rounded-2xl border border-white/[0.08] bg-black/15 p-4"
            >
              <fieldset disabled={busy || !canUsePasskeys}>
                <legend className="text-sm font-semibold text-slate-100">
                  Sign in with Passkey
                </legend>
                <p className="mt-2 text-[11px] leading-5 text-slate-500">
                  The browser lets you choose a discoverable MeterGate passkey.
                  No account ID or subject reference is submitted.
                </p>
                <button
                  type="submit"
                  className="mt-4 w-full rounded-xl border border-violet-300/20 bg-violet-300/[0.07] px-4 py-2.5 text-sm font-medium text-violet-100 transition hover:border-violet-300/35 hover:bg-violet-300/[0.11] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-200 disabled:cursor-wait disabled:opacity-50"
                >
                  {phase.startsWith("login")
                    ? "Signing in…"
                    : "Sign in with Passkey"}
                </button>
              </fieldset>
            </form>
          </div>
        </div>
      ) : null}

      {!supportChecked && state.kind !== "checking" ? (
        <p role="status" className="mt-4 text-xs text-slate-400">
          Checking browser passkey support…
        </p>
      ) : null}
      {currentPhaseMessage ? (
        <p role="status" aria-live="polite" className="mt-4 text-xs text-slate-300">
          {currentPhaseMessage}
        </p>
      ) : null}
      {failure ? <FailureNotice failure={failure} /> : null}
    </section>
  );
}
