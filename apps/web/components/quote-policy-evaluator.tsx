"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";

import { TrustedApproval } from "@/components/trusted-approval";

type JsonPrimitive = boolean | null | number | string;
type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

type PolicyQuoteContext = {
  id: string;
  quote_hash: string;
  merchant: {
    id: string;
    slug: string;
    name: string;
  };
  service: {
    id: string;
    slug: string;
    name: string;
    service_type: string;
  };
  pricing: {
    amount: number;
    currency: string;
    purchase_type: string;
  };
  expires_at: string;
  state: "active" | "expired";
};

type PolicyConstraints = {
  maximum_amount: number;
  allowed_currencies: string[] | null;
  allowed_merchant_ids: string[] | null;
  allowed_service_ids: string[] | null;
  allowed_service_types: string[] | null;
  allowed_purchase_types: string[] | null;
};

type PolicyResponse = {
  id: string;
  subject_ref: string;
  constraints: PolicyConstraints;
  issued_at: string;
  expires_at: string;
  state: "active" | "expired";
  policy_version: "1";
  policy_hash: string;
};

type PolicyEvaluationCheck = {
  rule: string;
  result: "pass" | "fail";
  reason_code: string;
  details: Record<string, JsonValue>;
};

type PolicyEvaluationResponse = {
  id: string;
  policy_id: string;
  quote_id: string;
  policy_hash: string;
  quote_hash: string;
  decision: "allow" | "deny";
  reason_codes: string[];
  checks: PolicyEvaluationCheck[];
  evaluated_at: string;
  created_at: string;
  evaluation_version: "1";
};

type PolicyDraft = {
  subjectRef: string;
  maximumAmount: string;
  expiresInSeconds: string;
  restrictCurrency: boolean;
  restrictMerchant: boolean;
  restrictService: boolean;
  restrictServiceType: boolean;
  restrictPurchaseType: boolean;
};

type DraftErrors = Partial<
  Record<"subjectRef" | "maximumAmount" | "expiresInSeconds", string>
>;

type CreationState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "error"; message: string }
  | { kind: "resolved"; policy: PolicyResponse };

type EvaluationState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "error"; message: string }
  | { kind: "resolved"; evaluation: PolicyEvaluationResponse };

type RequestKind = "creation" | "evaluation";

type PolicyCreatePayload = {
  subject_ref: string;
  maximum_amount: number;
  allowed_currencies: string[] | null;
  allowed_merchant_ids: string[] | null;
  allowed_service_ids: string[] | null;
  allowed_service_types: string[] | null;
  allowed_purchase_types: string[] | null;
  expires_in_seconds: number;
};

class PolicyRequestFailure extends Error {}

const REQUEST_TIMEOUT_MS = 8_000;
const MAX_EXPIRY_CHECK_INTERVAL_MS = 60_000;
const DEFAULT_POLICY_LIFETIME_SECONDS = "900";
const canonicalIntegerPattern = /^(0|[1-9]\d*)$/;
const integerFormatter = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 0,
});
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

function isJsonValue(value: unknown, depth = 0): value is JsonValue {
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "string"
  ) {
    return true;
  }

  if (typeof value === "number") {
    return Number.isFinite(value);
  }

  if (depth >= 64) {
    return false;
  }

  if (Array.isArray(value)) {
    return value.every((item) => isJsonValue(item, depth + 1));
  }

  return (
    isRecord(value) &&
    Object.values(value).every((item) => isJsonValue(item, depth + 1))
  );
}

function isStringListOrNull(value: unknown): value is string[] | null {
  if (value === null) {
    return true;
  }

  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every(isNonEmptyString) &&
    new Set(value).size === value.length
  );
}

function isPolicyConstraints(value: unknown): value is PolicyConstraints {
  return (
    isRecord(value) &&
    typeof value.maximum_amount === "number" &&
    Number.isSafeInteger(value.maximum_amount) &&
    value.maximum_amount >= 0 &&
    isStringListOrNull(value.allowed_currencies) &&
    isStringListOrNull(value.allowed_merchant_ids) &&
    isStringListOrNull(value.allowed_service_ids) &&
    isStringListOrNull(value.allowed_service_types) &&
    isStringListOrNull(value.allowed_purchase_types)
  );
}

function isPolicyResponse(value: unknown): value is PolicyResponse {
  return (
    isRecord(value) &&
    isNonEmptyString(value.id) &&
    isNonEmptyString(value.subject_ref) &&
    isPolicyConstraints(value.constraints) &&
    isTimestamp(value.issued_at) &&
    isTimestamp(value.expires_at) &&
    (value.state === "active" || value.state === "expired") &&
    value.policy_version === "1" &&
    isNonEmptyString(value.policy_hash)
  );
}

function isPolicyEvaluationCheck(
  value: unknown,
): value is PolicyEvaluationCheck {
  return (
    isRecord(value) &&
    isNonEmptyString(value.rule) &&
    (value.result === "pass" || value.result === "fail") &&
    isNonEmptyString(value.reason_code) &&
    isRecord(value.details) &&
    isJsonValue(value.details)
  );
}

function isPolicyEvaluationResponse(
  value: unknown,
): value is PolicyEvaluationResponse {
  return (
    isRecord(value) &&
    isNonEmptyString(value.id) &&
    isNonEmptyString(value.policy_id) &&
    isNonEmptyString(value.quote_id) &&
    isNonEmptyString(value.policy_hash) &&
    isNonEmptyString(value.quote_hash) &&
    (value.decision === "allow" || value.decision === "deny") &&
    Array.isArray(value.reason_codes) &&
    value.reason_codes.every(isNonEmptyString) &&
    Array.isArray(value.checks) &&
    value.checks.every(isPolicyEvaluationCheck) &&
    isTimestamp(value.evaluated_at) &&
    isTimestamp(value.created_at) &&
    value.evaluation_version === "1"
  );
}

function safeMessage(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }

  const message = value
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim();

  return message ? message.slice(0, 240) : null;
}

function safeDetailMessages(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.slice(0, 3).flatMap((item) => {
      if (!isRecord(item)) {
        return [];
      }

      const message = safeMessage(item.message) ?? safeMessage(item.msg);
      return message ? [message] : [];
    });
  }

  if (isRecord(value)) {
    const message = safeMessage(value.message);
    const reasonCode = safeMessage(value.reason_code);
    const issues = safeDetailMessages(value.issues);

    return [reasonCode, message, ...issues].filter(
      (item): item is string => item !== null,
    );
  }

  const message = safeMessage(value);
  return message ? [message] : [];
}

function apiErrorMessage(
  kind: RequestKind,
  status: number,
  body: unknown,
): string {
  const detail = isRecord(body)
    ? safeDetailMessages(body.detail).slice(0, 3).join(" ")
    : "";
  const subject = kind === "creation" ? "policy" : "policy evaluation";

  if (status === 404) {
    return `The ${subject} could not be completed because the policy or quote was not found. No payment was attempted.`;
  }

  if (status === 422) {
    return `The server rejected the ${subject} request.${detail ? ` ${detail}` : ""} No payment was attempted.`;
  }

  if (status >= 500) {
    return `The ${subject} service is temporarily unavailable. No payment was attempted.`;
  }

  return `The ${subject} request failed with HTTP ${status}. No payment was attempted.`;
}

function requestErrorMessage(kind: RequestKind, error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") {
    return kind === "creation"
      ? "The policy request timed out. It may still have been created; submitting again can create a separate policy. No payment was attempted."
      : "The policy evaluation timed out. It may still have been recorded; submitting again can create a separate evaluation. No payment was attempted.";
  }

  if (error instanceof TypeError) {
    return `The policy ${kind === "creation" ? "endpoint" : "evaluation endpoint"} could not be reached. Confirm FastAPI is running and CORS allows this origin. No payment was attempted.`;
  }

  if (error instanceof PolicyRequestFailure) {
    return error.message;
  }

  return `The ${kind === "creation" ? "policy" : "policy evaluation"} request could not be completed. No payment was attempted.`;
}

async function createPolicy(
  endpoint: string,
  payload: PolicyCreatePayload,
  signal: AbortSignal,
): Promise<PolicyResponse> {
  const response = await fetch(endpoint, {
    method: "POST",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    signal,
  });

  let body: unknown = null;

  try {
    body = await response.json();
  } catch {
    if (response.status === 201) {
      throw new PolicyRequestFailure(
        "The server created an unreadable policy response. No payment was attempted.",
      );
    }
  }

  if (response.status !== 201) {
    throw new PolicyRequestFailure(apiErrorMessage("creation", response.status, body));
  }

  if (!isPolicyResponse(body)) {
    throw new PolicyRequestFailure(
      "The server returned an unexpected policy response. No payment was attempted.",
    );
  }

  return body;
}

async function evaluatePolicy(
  endpoint: string,
  policyId: string,
  quoteId: string,
  signal: AbortSignal,
): Promise<PolicyEvaluationResponse> {
  const response = await fetch(endpoint, {
    method: "POST",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ policy_id: policyId, quote_id: quoteId }),
    signal,
  });

  let body: unknown = null;

  try {
    body = await response.json();
  } catch {
    if (response.status === 201) {
      throw new PolicyRequestFailure(
        "The server created an unreadable policy evaluation response. No payment was attempted.",
      );
    }
  }

  if (response.status !== 201) {
    throw new PolicyRequestFailure(
      apiErrorMessage("evaluation", response.status, body),
    );
  }

  if (
    !isPolicyEvaluationResponse(body) ||
    body.policy_id !== policyId ||
    body.quote_id !== quoteId
  ) {
    throw new PolicyRequestFailure(
      "The server returned an unexpected policy evaluation response. No payment was attempted.",
    );
  }

  return body;
}

function initialDraft(quote: PolicyQuoteContext): PolicyDraft {
  return {
    subjectRef: "",
    maximumAmount: quote.pricing.amount.toString(),
    expiresInSeconds: DEFAULT_POLICY_LIFETIME_SECONDS,
    restrictCurrency: true,
    restrictMerchant: true,
    restrictService: true,
    restrictServiceType: true,
    restrictPurchaseType: true,
  };
}

function parseCanonicalInteger(
  value: string,
  label: string,
  allowZero: boolean,
): { value: number } | { error: string } {
  const trimmed = value.trim();

  if (!canonicalIntegerPattern.test(trimmed)) {
    return {
      error: `${label} must be written as whole decimal digits without a sign, decimal point, or exponent.`,
    };
  }

  const parsed = Number(trimmed);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    return { error: `${label} must be a safe non-negative integer.` };
  }

  if (!allowZero && parsed === 0) {
    return { error: `${label} must be greater than zero.` };
  }

  return { value: parsed };
}

function formatToken(value: string): string {
  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

function formatMinorUnitAmount(
  amount: number,
  currency: string,
): { primary: string; context: string } {
  const normalizedCurrency = currency.toUpperCase();
  const minorUnits = integerFormatter.format(amount);

  if (normalizedCurrency === "INR") {
    const rupees = Math.trunc(amount / 100);
    const paise = amount % 100;
    return {
      primary: `₹${integerFormatter.format(rupees)}.${paise.toString().padStart(2, "0")}`,
      context: `${minorUnits} paise · exact integer minor units`,
    };
  }

  return {
    primary: `${normalizedCurrency} ${minorUnits}`,
    context: `${minorUnits} ${normalizedCurrency} minor units; no exponent assumed`,
  };
}

function ConstraintValue({ value }: { value: string[] | null }) {
  if (value === null) {
    return <span className="text-slate-500">Unconstrained</span>;
  }

  return <span className="break-all text-slate-300">{value.join(", ")}</span>;
}

function PolicySnapshot({
  policy,
  quote,
}: {
  policy: PolicyResponse;
  quote: PolicyQuoteContext;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [expiredByTime, setExpiredByTime] = useState(
    () => policy.state === "expired" || Date.now() >= Date.parse(policy.expires_at),
  );

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  useEffect(() => {
    if (policy.state === "expired" || Date.now() >= Date.parse(policy.expires_at)) {
      return;
    }

    let timeout: number;
    const checkExpiry = () => {
      const remaining = Date.parse(policy.expires_at) - Date.now();
      if (remaining <= 0) {
        setExpiredByTime(true);
        return;
      }

      timeout = window.setTimeout(
        checkExpiry,
        Math.min(remaining, MAX_EXPIRY_CHECK_INTERVAL_MS),
      );
    };

    checkExpiry();
    return () => window.clearTimeout(timeout);
  }, [policy.expires_at, policy.state]);

  const displayState =
    policy.state === "expired" || expiredByTime ? "expired" : "active";
  const maximum = formatMinorUnitAmount(
    policy.constraints.maximum_amount,
    quote.pricing.currency,
  );

  return (
    <section className="mt-4 rounded-2xl border border-violet-300/15 bg-violet-300/[0.035] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-violet-200/70">
            Locked server policy
          </p>
          <h6
            ref={headingRef}
            tabIndex={-1}
            className="mt-2 text-sm font-semibold text-white focus:outline-none"
          >
            Policy snapshot
          </h6>
        </div>
        <span className="rounded-full border border-white/10 bg-white/[0.035] px-2.5 py-1 text-[11px] font-medium text-slate-300">
          {formatToken(displayState)}
        </span>
      </div>

      <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2">
        <div>
          <dt className="text-slate-500">Policy ID</dt>
          <dd className="mt-1 break-all font-mono text-slate-300">{policy.id}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Subject</dt>
          <dd className="mt-1 break-all text-slate-300">{policy.subject_ref}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Maximum for this quote currency</dt>
          <dd className="mt-1 font-semibold text-slate-100">{maximum.primary}</dd>
          <dd className="mt-1 text-[10px] text-slate-500">{maximum.context}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Policy version</dt>
          <dd className="mt-1 text-slate-300">{policy.policy_version}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Issued</dt>
          <dd className="mt-1 text-slate-300">
            <time dateTime={policy.issued_at} title={policy.issued_at}>
              {dateTimeFormatter.format(new Date(policy.issued_at))}
            </time>
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Expires</dt>
          <dd className="mt-1 text-slate-300">
            <time dateTime={policy.expires_at} title={policy.expires_at}>
              {dateTimeFormatter.format(new Date(policy.expires_at))}
            </time>
          </dd>
        </div>
      </dl>

      <dl className="mt-4 space-y-2 border-t border-white/[0.06] pt-4 text-xs">
        <div>
          <dt className="text-slate-500">Allowed currencies</dt>
          <dd className="mt-1"><ConstraintValue value={policy.constraints.allowed_currencies} /></dd>
        </div>
        <div>
          <dt className="text-slate-500">Allowed merchants</dt>
          <dd className="mt-1"><ConstraintValue value={policy.constraints.allowed_merchant_ids} /></dd>
        </div>
        <div>
          <dt className="text-slate-500">Allowed services</dt>
          <dd className="mt-1"><ConstraintValue value={policy.constraints.allowed_service_ids} /></dd>
        </div>
        <div>
          <dt className="text-slate-500">Allowed service types</dt>
          <dd className="mt-1"><ConstraintValue value={policy.constraints.allowed_service_types} /></dd>
        </div>
        <div>
          <dt className="text-slate-500">Allowed purchase types</dt>
          <dd className="mt-1"><ConstraintValue value={policy.constraints.allowed_purchase_types} /></dd>
        </div>
      </dl>

      <div className="mt-4">
        <p className="text-xs text-slate-500">Policy hash</p>
        <p className="mt-1 break-all font-mono text-[10px] leading-4 text-slate-400">
          {policy.policy_hash}
        </p>
      </div>
    </section>
  );
}

function EvaluationResult({
  apiBaseEndpoint,
  evaluation,
  policy,
}: {
  apiBaseEndpoint: string;
  evaluation: PolicyEvaluationResponse;
  policy: PolicyResponse;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const allowed = evaluation.decision === "allow";

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <section
      className={`mt-4 rounded-2xl border p-4 ${
        allowed
          ? "border-emerald-300/15 bg-emerald-300/[0.04]"
          : "border-rose-300/15 bg-rose-300/[0.04]"
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-400">
            Server policy evaluation
          </p>
          <h6
            ref={headingRef}
            tabIndex={-1}
            className="mt-2 text-base font-semibold text-white focus:outline-none"
          >
            {allowed ? "Allowed by policy" : "Denied by policy"}
          </h6>
        </div>
        <span
          className={`rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.1em] ${
            allowed
              ? "border-emerald-300/20 text-emerald-200"
              : "border-rose-300/20 text-rose-100"
          }`}
        >
          {evaluation.decision}
        </span>
      </div>

      <p className="mt-3 rounded-xl border border-white/[0.07] bg-black/15 px-3 py-2.5 text-xs leading-5 text-slate-200">
        Policy approval does not execute or reserve payment. ALLOW means only
        that this quote fits the policy; it does not authorize a purchase,
        create an order, or move money.
      </p>

      <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2">
        <div>
          <dt className="text-slate-500">Evaluation ID</dt>
          <dd className="mt-1 break-all font-mono text-slate-300">{evaluation.id}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Evaluation version</dt>
          <dd className="mt-1 text-slate-300">{evaluation.evaluation_version}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Policy ID</dt>
          <dd className="mt-1 break-all font-mono text-[10px] text-slate-400">
            {evaluation.policy_id}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Quote ID</dt>
          <dd className="mt-1 break-all font-mono text-[10px] text-slate-400">
            {evaluation.quote_id}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Evaluated</dt>
          <dd className="mt-1 text-slate-300">
            <time dateTime={evaluation.evaluated_at} title={evaluation.evaluated_at}>
              {dateTimeFormatter.format(new Date(evaluation.evaluated_at))}
            </time>
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Recorded</dt>
          <dd className="mt-1 text-slate-300">
            <time dateTime={evaluation.created_at} title={evaluation.created_at}>
              {dateTimeFormatter.format(new Date(evaluation.created_at))}
            </time>
          </dd>
        </div>
      </dl>

      <div className="mt-4">
        <p className="text-xs text-slate-500">Reason codes</p>
        {evaluation.reason_codes.length > 0 ? (
          <ul className="mt-2 flex flex-wrap gap-2">
            {evaluation.reason_codes.map((reasonCode, index) => (
              <li
                key={`${reasonCode}-${index}`}
                className="rounded-full border border-white/10 bg-white/[0.035] px-2.5 py-1 font-mono text-[10px] text-slate-300"
              >
                {reasonCode}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-xs text-slate-400">No deny reason codes.</p>
        )}
      </div>

      <div className="mt-5 border-t border-white/[0.06] pt-4">
        <h6 className="text-xs font-semibold text-slate-200">
          Ordered policy checks
        </h6>
        <p className="mt-1 text-[11px] leading-5 text-slate-500">
          Only checks returned by the server are shown. An integrity denial may
          intentionally omit all non-integrity checks.
        </p>

        {evaluation.checks.length > 0 ? (
          <ol className="mt-3 space-y-3">
            {evaluation.checks.map((check, index) => (
              <li
                key={`${check.rule}-${index}`}
                className="rounded-xl border border-white/[0.07] bg-black/15 p-3"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="text-xs font-medium text-slate-200">
                      {index + 1}. {formatToken(check.rule)}
                    </p>
                    <p className="mt-1 break-all font-mono text-[10px] text-slate-500">
                      {check.reason_code}
                    </p>
                  </div>
                  <span
                    className={`text-[11px] font-semibold uppercase tracking-[0.1em] ${
                      check.result === "pass"
                        ? "text-emerald-200"
                        : "text-rose-200"
                    }`}
                  >
                    {check.result}
                  </span>
                </div>
                <pre className="mt-3 max-h-40 overflow-auto border-t border-white/[0.06] pt-3 font-mono text-[10px] leading-4 text-slate-500">
                  {JSON.stringify(check.details, null, 2)}
                </pre>
              </li>
            ))}
          </ol>
        ) : (
          <p className="mt-3 text-xs text-slate-400">
            The server returned no executed checks.
          </p>
        )}
      </div>

      <dl className="mt-4 space-y-3 border-t border-white/[0.06] pt-4 text-xs">
        <div>
          <dt className="text-slate-500">Policy hash</dt>
          <dd className="mt-1 break-all font-mono text-[10px] leading-4 text-slate-400">
            {evaluation.policy_hash}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Quote hash</dt>
          <dd className="mt-1 break-all font-mono text-[10px] leading-4 text-slate-400">
            {evaluation.quote_hash}
          </dd>
        </div>
      </dl>

      {allowed ? (
        <TrustedApproval
          key={`${evaluation.id}:${policy.subject_ref}`}
          apiBaseEndpoint={apiBaseEndpoint}
          evaluationId={evaluation.id}
          policySubjectRef={policy.subject_ref}
        />
      ) : (
        <p className="mt-4 rounded-xl border border-rose-300/15 bg-rose-300/[0.045] px-3 py-2.5 text-xs leading-5 text-rose-100/80">
          Denied evaluations cannot request trusted human approval. Create a
          policy and evaluation that satisfy every required check.
        </p>
      )}
    </section>
  );
}

function RestrictionCheckbox({
  checked,
  children,
  disabled,
  onChange,
}: {
  checked: boolean;
  children: React.ReactNode;
  disabled: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex items-start gap-3 rounded-xl border border-white/[0.07] bg-white/[0.02] px-3 py-2.5 text-xs leading-5 text-slate-300">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-1 size-3.5 accent-cyan-300"
      />
      <span>{children}</span>
    </label>
  );
}

export function QuotePolicyEvaluator({
  apiBaseEndpoint,
  createEndpoint,
  evaluationEndpoint,
  quote,
}: {
  apiBaseEndpoint: string;
  createEndpoint: string;
  evaluationEndpoint: string;
  quote: PolicyQuoteContext;
}) {
  const panelId = useId();
  const subjectId = useId();
  const subjectErrorId = useId();
  const amountId = useId();
  const amountHelpId = useId();
  const amountErrorId = useId();
  const expiryId = useId();
  const expiryHelpId = useId();
  const expiryErrorId = useId();
  const subjectRef = useRef<HTMLInputElement>(null);
  const amountRef = useRef<HTMLInputElement>(null);
  const expiryRef = useRef<HTMLInputElement>(null);
  const creationControllerRef = useRef<AbortController | null>(null);
  const evaluationControllerRef = useRef<AbortController | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [draft, setDraft] = useState<PolicyDraft>(() => initialDraft(quote));
  const [draftErrors, setDraftErrors] = useState<DraftErrors>({});
  const [creationState, setCreationState] = useState<CreationState>({
    kind: "idle",
  });
  const [evaluationState, setEvaluationState] = useState<EvaluationState>({
    kind: "idle",
  });

  useEffect(
    () => () => {
      creationControllerRef.current?.abort();
      evaluationControllerRef.current?.abort();
      creationControllerRef.current = null;
      evaluationControllerRef.current = null;
    },
    [],
  );

  const isCreating = creationState.kind === "submitting";
  const isEvaluating = evaluationState.kind === "submitting";
  const isBusy = isCreating || isEvaluating;
  const quoteAmount = formatMinorUnitAmount(
    quote.pricing.amount,
    quote.pricing.currency,
  );

  const clearRequestError = () => {
    if (creationState.kind === "error") {
      setCreationState({ kind: "idle" });
    }
  };

  const updateDraft = <Key extends keyof PolicyDraft>(
    key: Key,
    value: PolicyDraft[Key],
  ) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setDraftErrors((current) => {
      if (!(key in current)) {
        return current;
      }

      const next = { ...current };
      delete next[key as keyof DraftErrors];
      return next;
    });
    clearRequestError();
  };

  const loadMatchingConstraints = () => {
    setDraft((current) => ({
      ...current,
      maximumAmount: quote.pricing.amount.toString(),
      restrictCurrency: true,
      restrictMerchant: true,
      restrictService: true,
      restrictServiceType: true,
      restrictPurchaseType: true,
    }));
    setDraftErrors({});
    clearRequestError();
  };

  const setLimitBelowQuote = () => {
    if (quote.pricing.amount === 0) {
      return;
    }

    setDraft((current) => ({
      ...current,
      maximumAmount: (quote.pricing.amount - 1).toString(),
      restrictCurrency: true,
      restrictMerchant: true,
      restrictService: true,
      restrictServiceType: true,
      restrictPurchaseType: true,
    }));
    setDraftErrors({});
    clearRequestError();
  };

  const submitPolicy = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    const errors: DraftErrors = {};
    const normalizedSubject = draft.subjectRef.trim();
    if (!normalizedSubject) {
      errors.subjectRef = "Enter a subject reference for this policy.";
    }

    const maximumAmount = parseCanonicalInteger(
      draft.maximumAmount,
      "Maximum amount",
      true,
    );
    if ("error" in maximumAmount) {
      errors.maximumAmount = maximumAmount.error;
    }

    const expiresInSeconds = parseCanonicalInteger(
      draft.expiresInSeconds,
      "Policy lifetime",
      false,
    );
    if ("error" in expiresInSeconds) {
      errors.expiresInSeconds = expiresInSeconds.error;
    }

    if (Object.keys(errors).length > 0) {
      setDraftErrors(errors);
      if (errors.subjectRef) {
        subjectRef.current?.focus();
      } else if (errors.maximumAmount) {
        amountRef.current?.focus();
      } else {
        expiryRef.current?.focus();
      }
      return;
    }

    if ("error" in maximumAmount || "error" in expiresInSeconds) {
      return;
    }

    const payload: PolicyCreatePayload = {
      subject_ref: normalizedSubject,
      maximum_amount: maximumAmount.value,
      allowed_currencies: draft.restrictCurrency
        ? [quote.pricing.currency]
        : null,
      allowed_merchant_ids: draft.restrictMerchant ? [quote.merchant.id] : null,
      allowed_service_ids: draft.restrictService ? [quote.service.id] : null,
      allowed_service_types: draft.restrictServiceType
        ? [quote.service.service_type]
        : null,
      allowed_purchase_types: draft.restrictPurchaseType
        ? [quote.pricing.purchase_type]
        : null,
      expires_in_seconds: expiresInSeconds.value,
    };

    const controller = new AbortController();
    creationControllerRef.current = controller;
    const timeout = window.setTimeout(
      () => controller.abort(),
      REQUEST_TIMEOUT_MS,
    );
    setDraftErrors({});
    setCreationState({ kind: "submitting" });
    setEvaluationState({ kind: "idle" });

    try {
      const policy = await createPolicy(createEndpoint, payload, controller.signal);
      if (creationControllerRef.current === controller) {
        setCreationState({ kind: "resolved", policy });
      }
    } catch (error: unknown) {
      if (creationControllerRef.current === controller) {
        setCreationState({
          kind: "error",
          message: requestErrorMessage("creation", error),
        });
      }
    } finally {
      window.clearTimeout(timeout);
      if (creationControllerRef.current === controller) {
        creationControllerRef.current = null;
      }
    }
  };

  const submitEvaluation = async () => {
    if (creationState.kind !== "resolved") {
      return;
    }

    const policy = creationState.policy;
    const controller = new AbortController();
    evaluationControllerRef.current = controller;
    const timeout = window.setTimeout(
      () => controller.abort(),
      REQUEST_TIMEOUT_MS,
    );
    setEvaluationState({ kind: "submitting" });

    try {
      const evaluation = await evaluatePolicy(
        evaluationEndpoint,
        policy.id,
        quote.id,
        controller.signal,
      );
      if (evaluationControllerRef.current === controller) {
        setEvaluationState({ kind: "resolved", evaluation });
      }
    } catch (error: unknown) {
      if (evaluationControllerRef.current === controller) {
        setEvaluationState({
          kind: "error",
          message: requestErrorMessage("evaluation", error),
        });
      }
    } finally {
      window.clearTimeout(timeout);
      if (evaluationControllerRef.current === controller) {
        evaluationControllerRef.current = null;
      }
    }
  };

  const startAnotherPolicy = () => {
    setDraft(initialDraft(quote));
    setDraftErrors({});
    setCreationState({ kind: "idle" });
    setEvaluationState({ kind: "idle" });
    window.setTimeout(() => subjectRef.current?.focus(), 0);
  };

  const toggleLabel = expanded ? "Hide policy check" : "Check purchase policy";

  return (
    <div className="mt-5 border-t border-white/[0.07] pt-5">
      <button
        type="button"
        aria-controls={panelId}
        aria-expanded={expanded}
        disabled={isBusy}
        onClick={() => setExpanded((current) => !current)}
        className="w-full rounded-xl border border-violet-300/20 bg-violet-300/[0.06] px-4 py-2.5 text-sm font-medium text-violet-100 transition hover:border-violet-300/35 hover:bg-violet-300/[0.1] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-200 disabled:cursor-wait disabled:opacity-60"
      >
        {isCreating
          ? "Creating policy…"
          : isEvaluating
            ? "Evaluating policy…"
            : toggleLabel}
      </button>

      {expanded ? (
        <div id={panelId} className="mt-4" aria-busy={isBusy}>
          <div>
            <p className="text-xs font-semibold text-slate-200">
              Deterministic quote policy
            </p>
            <p className="mt-1 text-[11px] leading-5 text-slate-500">
              Create server-owned limits, then ask MeterGate to evaluate this
              immutable quote. Policy creation and evaluation do not move money.
            </p>
          </div>

          {creationState.kind !== "resolved" ? (
            <form onSubmit={submitPolicy} className="mt-4" noValidate>
              <fieldset disabled={isCreating} className="space-y-4">
                <legend className="sr-only">Create quote policy</legend>

                <div className="rounded-xl border border-cyan-300/10 bg-cyan-300/[0.03] p-3">
                  <p className="text-[11px] leading-5 text-slate-400">
                    Developer helpers only populate the visible fields below. They
                    never choose or predict the server decision.
                  </p>
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    <button
                      type="button"
                      onClick={loadMatchingConstraints}
                      className="rounded-lg border border-white/10 bg-white/[0.035] px-3 py-2 text-xs font-medium text-slate-200 transition hover:border-cyan-300/25 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200"
                    >
                      Load matching constraints
                    </button>
                    <button
                      type="button"
                      onClick={setLimitBelowQuote}
                      disabled={quote.pricing.amount === 0}
                      title={
                        quote.pricing.amount === 0
                          ? "A zero-value quote has no lower non-negative amount."
                          : undefined
                      }
                      className="rounded-lg border border-white/10 bg-white/[0.035] px-3 py-2 text-xs font-medium text-slate-200 transition hover:border-rose-300/25 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-200 disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      Set limit below quote
                    </button>
                  </div>
                </div>

                <div>
                  <label htmlFor={subjectId} className="text-xs font-medium text-slate-200">
                    Subject reference
                  </label>
                  <p className="mt-1 text-[11px] leading-5 text-slate-500">
                    A non-secret buyer or agent reference for this policy.
                  </p>
                  <input
                    ref={subjectRef}
                    id={subjectId}
                    type="text"
                    value={draft.subjectRef}
                    required
                    autoComplete="off"
                    aria-invalid={draftErrors.subjectRef ? true : undefined}
                    aria-describedby={draftErrors.subjectRef ? subjectErrorId : undefined}
                    onChange={(event) => updateDraft("subjectRef", event.target.value)}
                    className="mt-2 w-full rounded-xl border border-white/10 bg-black/25 px-3 py-2.5 text-sm text-slate-200 outline-none transition focus:border-cyan-300/35 focus:ring-2 focus:ring-cyan-300/10 disabled:cursor-wait disabled:opacity-60"
                  />
                  {draftErrors.subjectRef ? (
                    <p id={subjectErrorId} role="alert" className="mt-2 text-xs text-rose-200">
                      {draftErrors.subjectRef}
                    </p>
                  ) : null}
                </div>

                <div>
                  <label htmlFor={amountId} className="text-xs font-medium text-slate-200">
                    Maximum amount ({quote.pricing.currency.toUpperCase()} integer minor units)
                  </label>
                  <p id={amountHelpId} className="mt-1 text-[11px] leading-5 text-slate-500">
                    Current quote: {quoteAmount.primary} · {quoteAmount.context}.
                  </p>
                  <input
                    ref={amountRef}
                    id={amountId}
                    type="text"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    value={draft.maximumAmount}
                    required
                    aria-invalid={draftErrors.maximumAmount ? true : undefined}
                    aria-describedby={`${amountHelpId}${
                      draftErrors.maximumAmount ? ` ${amountErrorId}` : ""
                    }`}
                    onChange={(event) => updateDraft("maximumAmount", event.target.value)}
                    className="mt-2 w-full rounded-xl border border-white/10 bg-black/25 px-3 py-2.5 font-mono text-sm text-slate-200 outline-none transition focus:border-cyan-300/35 focus:ring-2 focus:ring-cyan-300/10 disabled:cursor-wait disabled:opacity-60"
                  />
                  {draftErrors.maximumAmount ? (
                    <p id={amountErrorId} role="alert" className="mt-2 text-xs text-rose-200">
                      {draftErrors.maximumAmount}
                    </p>
                  ) : null}
                </div>

                <div>
                  <label htmlFor={expiryId} className="text-xs font-medium text-slate-200">
                    Policy lifetime (seconds)
                  </label>
                  <p id={expiryHelpId} className="mt-1 text-[11px] leading-5 text-slate-500">
                    The server validates the lifetime and derives the exact expiry.
                  </p>
                  <input
                    ref={expiryRef}
                    id={expiryId}
                    type="text"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    value={draft.expiresInSeconds}
                    required
                    aria-invalid={draftErrors.expiresInSeconds ? true : undefined}
                    aria-describedby={`${expiryHelpId}${
                      draftErrors.expiresInSeconds ? ` ${expiryErrorId}` : ""
                    }`}
                    onChange={(event) => updateDraft("expiresInSeconds", event.target.value)}
                    className="mt-2 w-full rounded-xl border border-white/10 bg-black/25 px-3 py-2.5 font-mono text-sm text-slate-200 outline-none transition focus:border-cyan-300/35 focus:ring-2 focus:ring-cyan-300/10 disabled:cursor-wait disabled:opacity-60"
                  />
                  {draftErrors.expiresInSeconds ? (
                    <p id={expiryErrorId} role="alert" className="mt-2 text-xs text-rose-200">
                      {draftErrors.expiresInSeconds}
                    </p>
                  ) : null}
                </div>

                <fieldset className="space-y-2">
                  <legend className="text-xs font-medium text-slate-200">
                    Quote-scoped allowlists
                  </legend>
                  <p className="pb-1 text-[11px] leading-5 text-slate-500">
                    Checked sends the displayed server value as a one-item allowlist.
                    Unchecked sends null, meaning unconstrained.
                  </p>
                  <RestrictionCheckbox
                    checked={draft.restrictCurrency}
                    disabled={isCreating}
                    onChange={(checked) => updateDraft("restrictCurrency", checked)}
                  >
                    Only currency <span className="font-mono text-slate-400">{quote.pricing.currency}</span>
                  </RestrictionCheckbox>
                  <RestrictionCheckbox
                    checked={draft.restrictMerchant}
                    disabled={isCreating}
                    onChange={(checked) => updateDraft("restrictMerchant", checked)}
                  >
                    Only merchant {quote.merchant.name} · <span className="break-all font-mono text-slate-400">{quote.merchant.id}</span>
                  </RestrictionCheckbox>
                  <RestrictionCheckbox
                    checked={draft.restrictService}
                    disabled={isCreating}
                    onChange={(checked) => updateDraft("restrictService", checked)}
                  >
                    Only service {quote.service.name} · <span className="break-all font-mono text-slate-400">{quote.service.id}</span>
                  </RestrictionCheckbox>
                  <RestrictionCheckbox
                    checked={draft.restrictServiceType}
                    disabled={isCreating}
                    onChange={(checked) => updateDraft("restrictServiceType", checked)}
                  >
                    Only service type <span className="font-mono text-slate-400">{quote.service.service_type}</span>
                  </RestrictionCheckbox>
                  <RestrictionCheckbox
                    checked={draft.restrictPurchaseType}
                    disabled={isCreating}
                    onChange={(checked) => updateDraft("restrictPurchaseType", checked)}
                  >
                    Only purchase type <span className="font-mono text-slate-400">{quote.pricing.purchase_type}</span>
                  </RestrictionCheckbox>
                </fieldset>

                <button
                  type="submit"
                  disabled={isCreating}
                  className="w-full rounded-xl border border-violet-300/20 bg-violet-300/[0.07] px-4 py-2.5 text-sm font-medium text-violet-100 transition hover:border-violet-300/35 hover:bg-violet-300/[0.11] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-200 disabled:cursor-wait disabled:opacity-60"
                >
                  {isCreating ? "Creating server policy…" : "Create policy"}
                </button>
              </fieldset>
            </form>
          ) : (
            <>
              <PolicySnapshot policy={creationState.policy} quote={quote} />

              {evaluationState.kind !== "resolved" ? (
                <button
                  type="button"
                  disabled={isEvaluating}
                  onClick={submitEvaluation}
                  className="mt-3 w-full rounded-xl border border-cyan-300/20 bg-cyan-300/[0.07] px-4 py-2.5 text-sm font-medium text-cyan-100 transition hover:border-cyan-300/35 hover:bg-cyan-300/[0.11] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-wait disabled:opacity-60"
                >
                  {isEvaluating ? "Evaluating server policy…" : "Evaluate this quote"}
                </button>
              ) : null}
            </>
          )}

          {isCreating ? (
            <p role="status" className="mt-3 text-xs leading-5 text-slate-400">
              Creating a policy record only. No payment is being attempted.
            </p>
          ) : null}

          {creationState.kind === "error" ? (
            <p role="alert" className="mt-3 rounded-xl border border-rose-300/10 bg-rose-300/[0.05] px-3 py-2.5 text-xs leading-5 text-rose-100/80">
              {creationState.message}
            </p>
          ) : null}

          {isEvaluating ? (
            <p role="status" className="mt-3 text-xs leading-5 text-slate-400">
              Running deterministic server checks. No payment or approval is being attempted.
            </p>
          ) : null}

          {evaluationState.kind === "error" ? (
            <p role="alert" className="mt-3 rounded-xl border border-rose-300/10 bg-rose-300/[0.05] px-3 py-2.5 text-xs leading-5 text-rose-100/80">
              {evaluationState.message}
            </p>
          ) : null}

          {evaluationState.kind === "resolved" &&
          creationState.kind === "resolved" ? (
            <EvaluationResult
              apiBaseEndpoint={apiBaseEndpoint}
              evaluation={evaluationState.evaluation}
              policy={creationState.policy}
            />
          ) : null}

          {creationState.kind === "resolved" ? (
            <button
              type="button"
              disabled={isEvaluating}
              onClick={startAnotherPolicy}
              className="mt-3 w-full rounded-xl border border-white/10 bg-white/[0.035] px-4 py-2.5 text-sm font-medium text-slate-300 transition hover:border-white/20 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-wait disabled:opacity-60"
            >
              Create another policy
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
