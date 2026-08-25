"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";

import {
  createPasskeyCredential,
  getPasskeyCredential,
  passkeySupportError,
  WebAuthnBrowserFailure,
} from "@/lib/webauthn";

type JsonPrimitive = boolean | null | number | string;
type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

type ApprovalIdentity = {
  id: string;
  subject_ref: string;
  display_name: string;
  status: "active" | "disabled";
  created_at: string;
  has_passkey?: boolean;
  has_registered_passkey?: boolean;
  passkey_registered?: boolean;
  credential_count?: number;
  passkey_count?: number;
};

type CeremonyOptions = {
  challenge_id: string;
  public_key: Record<string, unknown>;
  expires_at: string;
};

type ApprovalReview = {
  review_version: string;
  evaluation_id: string;
  evaluation_version: string;
  policy_id: string;
  policy_hash: string;
  quote_id: string;
  quote_hash: string;
  subject_ref: string;
  approval_identity_id: string;
  merchant: { id: string; name: string };
  service: { id: string; name: string };
  amount: number;
  currency: string;
  purchase_type: string;
  quote_expires_at: string;
  policy_expires_at: string;
  policy_checks: Array<{
    rule: string;
    result: "pass" | "fail";
    reason_code: string;
    details: Record<string, JsonValue>;
    explanation: string;
  }>;
};

type ApprovalChallenge = CeremonyOptions & {
  review: ApprovalReview;
  review_hash: string;
};

type Authorization = {
  id: string;
  state: "active" | "expired";
  subject_ref: string;
  merchantName: string;
  serviceName: string;
  amount: number;
  currency: string;
  purchase_type: string;
  evaluation_id: string;
  policy_hash: string;
  quote_hash: string;
  review_hash: string;
  authorized_at: string;
  expires_at: string;
  authorization_hash: string;
};

type DisplayFailure = {
  code: string;
  message: string;
};

type IdentityPhase = "idle" | "creating" | "loading" | "resolved";
type RegistrationPhase = "idle" | "options" | "browser" | "verifying";
type ApprovalPhase =
  | "idle"
  | "preparing"
  | "ready"
  | "browser"
  | "verifying"
  | "authorized";

class ApprovalRequestFailure extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApprovalRequestFailure";
  }
}

const REQUEST_TIMEOUT_MS = 10_000;
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

function detailMessage(value: unknown): string | null {
  if (Array.isArray(value)) {
    for (const item of value.slice(0, 3)) {
      if (isRecord(item)) {
        const message = safeMessage(item.message) ?? safeMessage(item.msg);
        if (message) {
          return message;
        }
      }
    }
    return null;
  }

  if (isRecord(value)) {
    return safeMessage(value.message) ?? detailMessage(value.issues);
  }

  return safeMessage(value);
}

function apiFailure(status: number, body: unknown): ApprovalRequestFailure {
  const code =
    (isRecord(body) ? safeMessage(body.reason_code) : null) ??
    `APPROVAL_HTTP_${status}`;
  const message =
    (isRecord(body) ? detailMessage(body.detail) : null) ??
    (status >= 500
      ? "The trusted approval service is temporarily unavailable."
      : `The trusted approval request failed with HTTP ${status}.`);

  return new ApprovalRequestFailure(code, message);
}

async function requestJson(
  endpoint: string,
  method: "GET" | "POST",
  payload?: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );

  try {
    const response = await fetch(endpoint, {
      method,
      cache: "no-store",
      headers:
        method === "POST"
          ? {
              Accept: "application/json",
              "Content-Type": "application/json",
            }
          : { Accept: "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload),
      signal: controller.signal,
    });

    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      if (response.ok) {
        throw new ApprovalRequestFailure(
          "APPROVAL_RESPONSE_INVALID",
          "The trusted approval service returned unreadable JSON.",
        );
      }
    }

    if (!response.ok) {
      throw apiFailure(response.status, body);
    }

    if (!isRecord(body)) {
      throw new ApprovalRequestFailure(
        "APPROVAL_RESPONSE_INVALID",
        "The trusted approval service returned an unexpected response.",
      );
    }

    return body;
  } catch (error: unknown) {
    if (error instanceof ApprovalRequestFailure) {
      throw error;
    }

    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApprovalRequestFailure(
        "APPROVAL_REQUEST_TIMEOUT",
        "The trusted approval request timed out. Its final server state may be unknown.",
      );
    }

    if (error instanceof TypeError) {
      throw new ApprovalRequestFailure(
        "APPROVAL_SERVICE_UNAVAILABLE",
        "The trusted approval API could not be reached. Confirm FastAPI is running and CORS permits this origin.",
      );
    }

    throw new ApprovalRequestFailure(
      "APPROVAL_REQUEST_FAILED",
      "The trusted approval request could not be completed.",
    );
  } finally {
    window.clearTimeout(timeout);
  }
}

function parseIdentity(value: unknown): ApprovalIdentity | null {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.id) ||
    !isNonEmptyString(value.subject_ref) ||
    !isNonEmptyString(value.display_name) ||
    (value.status !== "active" && value.status !== "disabled") ||
    !isTimestamp(value.created_at)
  ) {
    return null;
  }

  const optionalBooleans = [
    value.has_passkey,
    value.has_registered_passkey,
    value.passkey_registered,
  ];
  const optionalCounts = [value.credential_count, value.passkey_count];
  if (
    optionalBooleans.some(
      (item) => item !== undefined && typeof item !== "boolean",
    ) ||
    optionalCounts.some(
      (item) =>
        item !== undefined &&
        (typeof item !== "number" || !Number.isSafeInteger(item) || item < 0),
    )
  ) {
    return null;
  }

  return value as ApprovalIdentity;
}

function identityHasPasskey(identity: ApprovalIdentity): boolean {
  return (
    identity.has_passkey === true ||
    identity.has_registered_passkey === true ||
    identity.passkey_registered === true ||
    (identity.credential_count ?? 0) > 0 ||
    (identity.passkey_count ?? 0) > 0
  );
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

function parseReview(value: unknown): ApprovalReview | null {
  if (!isRecord(value)) {
    return null;
  }

  const merchant = value.merchant;
  const service = value.service;
  const policyChecks = value.policy_checks;

  const strings = [
    value.review_version,
    value.evaluation_id,
    value.evaluation_version,
    value.policy_id,
    value.policy_hash,
    value.quote_id,
    value.quote_hash,
    value.subject_ref,
    value.approval_identity_id,
    value.currency,
    value.purchase_type,
  ];

  if (
    !strings.every(isNonEmptyString) ||
    !isRecord(merchant) ||
    !isNonEmptyString(merchant.id) ||
    !isNonEmptyString(merchant.name) ||
    !isRecord(service) ||
    !isNonEmptyString(service.id) ||
    !isNonEmptyString(service.name) ||
    typeof value.amount !== "number" ||
    !Number.isSafeInteger(value.amount) ||
    value.amount < 0 ||
    !isTimestamp(value.quote_expires_at) ||
    !isTimestamp(value.policy_expires_at) ||
    !Array.isArray(policyChecks) ||
    policyChecks.length === 0 ||
    !policyChecks.every(
      (check) =>
        isRecord(check) &&
        isNonEmptyString(check.rule) &&
        (check.result === "pass" || check.result === "fail") &&
        isNonEmptyString(check.reason_code) &&
        isRecord(check.details) &&
        isJsonValue(check.details) &&
        isNonEmptyString(check.explanation),
    )
  ) {
    return null;
  }

  return value as ApprovalReview;
}

function parseApprovalChallenge(value: unknown): ApprovalChallenge | null {
  const options = parseCeremonyOptions(value);
  if (!options || !isRecord(value)) {
    return null;
  }

  const review = parseReview(value.review);
  if (
    !review ||
    !isNonEmptyString(value.review_hash) ||
    options.public_key.userVerification !== "required"
  ) {
    return null;
  }

  return { ...options, review, review_hash: value.review_hash };
}

function entityName(
  value: Record<string, unknown>,
  field: "merchant" | "service",
): string | null {
  const direct = safeMessage(value[`${field}_name`]);
  if (direct) {
    return direct;
  }

  const nested = value[field];
  if (isRecord(nested)) {
    return safeMessage(nested.name);
  }

  return safeMessage(nested);
}

function parseAuthorization(value: unknown): Authorization | null {
  if (!isRecord(value)) {
    return null;
  }

  const merchantName = entityName(value, "merchant");
  const serviceName = entityName(value, "service");
  if (
    !isNonEmptyString(value.id) ||
    (value.state !== "active" && value.state !== "expired") ||
    !isNonEmptyString(value.subject_ref) ||
    !merchantName ||
    !serviceName ||
    typeof value.amount !== "number" ||
    !Number.isSafeInteger(value.amount) ||
    value.amount < 0 ||
    !isNonEmptyString(value.currency) ||
    !isNonEmptyString(value.purchase_type) ||
    !isNonEmptyString(value.evaluation_id) ||
    !isNonEmptyString(value.policy_hash) ||
    !isNonEmptyString(value.quote_hash) ||
    !isNonEmptyString(value.review_hash) ||
    !isTimestamp(value.authorized_at) ||
    !isTimestamp(value.expires_at) ||
    !isNonEmptyString(value.authorization_hash)
  ) {
    return null;
  }

  return {
    id: value.id,
    state: value.state,
    subject_ref: value.subject_ref,
    merchantName,
    serviceName,
    amount: value.amount,
    currency: value.currency,
    purchase_type: value.purchase_type,
    evaluation_id: value.evaluation_id,
    policy_hash: value.policy_hash,
    quote_hash: value.quote_hash,
    review_hash: value.review_hash,
    authorized_at: value.authorized_at,
    expires_at: value.expires_at,
    authorization_hash: value.authorization_hash,
  };
}

function failureFrom(error: unknown, stage: "approval" | "registration"): DisplayFailure {
  if (error instanceof ApprovalRequestFailure) {
    return { code: error.code, message: error.message };
  }

  if (error instanceof WebAuthnBrowserFailure) {
    const code =
      error.kind === "cancelled"
        ? stage === "registration"
          ? "APPROVAL_PASSKEY_REGISTRATION_CANCELLED"
          : "APPROVAL_CANCELLED"
        : error.kind === "unsupported"
          ? "APPROVAL_PASSKEY_UNSUPPORTED"
          : "APPROVAL_WEBAUTHN_VERIFICATION_FAILED";
    return { code, message: error.message };
  }

  return {
    code: "APPROVAL_REQUEST_FAILED",
    message: "The trusted approval operation could not be completed.",
  };
}

function formatToken(value: string): string {
  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

function formatAmount(amount: number, currency: string): string {
  const normalizedCurrency = currency.toUpperCase();
  if (normalizedCurrency === "INR") {
    const rupees = Math.trunc(amount / 100);
    const paise = amount % 100;
    return `₹${integerFormatter.format(rupees)}.${paise.toString().padStart(2, "0")}`;
  }

  return `${normalizedCurrency} ${integerFormatter.format(amount)}`;
}

function FailureNotice({ failure }: { failure: DisplayFailure }) {
  return (
    <div
      role="alert"
      className="mt-3 rounded-xl border border-rose-300/15 bg-rose-300/[0.05] px-3 py-3"
    >
      <p className="font-mono text-[10px] text-rose-200/80">{failure.code}</p>
      <p className="mt-1 text-xs leading-5 text-rose-100/85">
        {failure.message}
      </p>
    </div>
  );
}

function ReviewPanel({
  challenge,
}: {
  challenge: ApprovalChallenge;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const review = challenge.review;
  const explanations = review.policy_checks
    .filter((check) => check.result === "pass")
    .map((check) => check.explanation);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <section className="mt-4 rounded-2xl border border-amber-300/20 bg-amber-300/[0.04] p-4">
      <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-amber-200/80">
        Challenge-bound review
      </p>
      <h6
        ref={headingRef}
        tabIndex={-1}
        className="mt-2 text-base font-semibold text-white focus:outline-none"
      >
        Confirm these exact purchase terms
      </h6>

      <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2">
        <div>
          <dt className="text-slate-500">Merchant</dt>
          <dd className="mt-1 text-sm font-medium text-slate-100">
            {review.merchant.name}
          </dd>
          <dd className="mt-1 break-all font-mono text-[10px] text-slate-500">
            {review.merchant.id}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Service</dt>
          <dd className="mt-1 text-sm font-medium text-slate-100">
            {review.service.name}
          </dd>
          <dd className="mt-1 break-all font-mono text-[10px] text-slate-500">
            {review.service.id}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Amount</dt>
          <dd className="mt-1 text-lg font-semibold text-white">
            {formatAmount(review.amount, review.currency)}{" "}
            <span className="text-xs font-medium text-slate-400">
              {review.currency.toUpperCase()}
            </span>
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Purchase</dt>
          <dd className="mt-1 text-slate-200">
            {formatToken(review.purchase_type)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Quote expires</dt>
          <dd className="mt-1 text-slate-300">
            <time dateTime={review.quote_expires_at} title={review.quote_expires_at}>
              {dateTimeFormatter.format(new Date(review.quote_expires_at))}
            </time>
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Policy expires</dt>
          <dd className="mt-1 text-slate-300">
            <time
              dateTime={review.policy_expires_at}
              title={review.policy_expires_at}
            >
              {dateTimeFormatter.format(new Date(review.policy_expires_at))}
            </time>
          </dd>
        </div>
      </dl>

      <div className="mt-4 border-t border-white/[0.07] pt-4">
        <p className="text-xs font-medium text-slate-200">Policy</p>
        <ul className="mt-2 space-y-2 text-xs text-slate-300">
          {explanations.map((explanation) => (
            <li key={explanation} className="flex gap-2">
              <span aria-hidden="true" className="text-emerald-300">
                ✓
              </span>
              <span>{explanation}</span>
            </li>
          ))}
        </ul>
      </div>

      <dl className="mt-4 space-y-2 border-t border-white/[0.07] pt-4 text-xs">
        <div>
          <dt className="text-slate-500">Review hash</dt>
          <dd className="mt-1 break-all font-mono text-[10px] leading-4 text-slate-400">
            {challenge.review_hash}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Challenge expires</dt>
          <dd className="mt-1 text-slate-300">
            <time dateTime={challenge.expires_at} title={challenge.expires_at}>
              {dateTimeFormatter.format(new Date(challenge.expires_at))}
            </time>
          </dd>
        </div>
      </dl>
    </section>
  );
}

function AuthorizationPanel({ authorization }: { authorization: Authorization }) {
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <section className="mt-4 rounded-2xl border border-emerald-300/20 bg-emerald-300/[0.05] p-4">
      <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-emerald-200/75">
        Purchase authorization
      </p>
      <h6
        ref={headingRef}
        tabIndex={-1}
        className="mt-2 text-lg font-semibold text-white focus:outline-none"
      >
        AUTHORIZED
      </h6>
      <p className="mt-1 font-mono text-[10px] text-emerald-200/70">
        APPROVAL_AUTHORIZED
      </p>

      <p className="mt-3 rounded-xl border border-emerald-300/15 bg-black/15 px-3 py-2.5 text-xs leading-5 text-emerald-50/90">
        Human verification: Passkey verified
      </p>

      <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2">
        <div>
          <dt className="text-slate-500">Authorization</dt>
          <dd className="mt-1 break-all font-mono text-slate-200">
            {authorization.id}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">State</dt>
          <dd className="mt-1 text-slate-200">
            {formatToken(authorization.state)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Amount</dt>
          <dd className="mt-1 text-base font-semibold text-white">
            {formatAmount(authorization.amount, authorization.currency)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Purchase</dt>
          <dd className="mt-1 text-slate-200">
            {formatToken(authorization.purchase_type)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Merchant</dt>
          <dd className="mt-1 text-slate-200">{authorization.merchantName}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Service</dt>
          <dd className="mt-1 text-slate-200">{authorization.serviceName}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-slate-500">Valid until</dt>
          <dd className="mt-1 text-slate-300">
            <time dateTime={authorization.expires_at} title={authorization.expires_at}>
              {dateTimeFormatter.format(new Date(authorization.expires_at))}
            </time>
          </dd>
        </div>
      </dl>

      <div className="mt-4 border-t border-white/[0.07] pt-4">
        <p className="text-xs text-slate-500">Authorization hash</p>
        <p className="mt-1 break-all font-mono text-[10px] leading-4 text-slate-400">
          {authorization.authorization_hash}
        </p>
      </div>

      <p className="mt-4 rounded-xl border border-white/[0.08] bg-black/20 px-3 py-2.5 text-xs font-medium leading-5 text-white">
        No payment has been created or executed yet.
      </p>
    </section>
  );
}

export function TrustedApproval({
  apiBaseEndpoint,
  evaluationId,
  policySubjectRef,
}: {
  apiBaseEndpoint: string;
  evaluationId: string;
  policySubjectRef: string;
}) {
  const displayNameId = useId();
  const existingIdentityId = useId();
  const identityHeadingRef = useRef<HTMLHeadingElement>(null);
  const passkeyStatusRef = useRef<HTMLDivElement>(null);
  const [supportFailure, setSupportFailure] = useState<DisplayFailure | null>(
    null,
  );
  const [supportChecked, setSupportChecked] = useState(false);
  const [displayName, setDisplayName] = useState("Local MeterGate User");
  const [existingIdentity, setExistingIdentity] = useState("");
  const [identity, setIdentity] = useState<ApprovalIdentity | null>(null);
  const [identityPhase, setIdentityPhase] = useState<IdentityPhase>("idle");
  const [identityFailure, setIdentityFailure] = useState<DisplayFailure | null>(
    null,
  );
  const [passkeyRegistered, setPasskeyRegistered] = useState(false);
  const [registrationPhase, setRegistrationPhase] =
    useState<RegistrationPhase>("idle");
  const [registrationFailure, setRegistrationFailure] =
    useState<DisplayFailure | null>(null);
  const [approvalPhase, setApprovalPhase] = useState<ApprovalPhase>("idle");
  const [approvalFailure, setApprovalFailure] =
    useState<DisplayFailure | null>(null);
  const [challenge, setChallenge] = useState<ApprovalChallenge | null>(null);
  const [challengeExpired, setChallengeExpired] = useState(false);
  const [authorization, setAuthorization] = useState<Authorization | null>(null);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      const message = passkeySupportError();
      setSupportChecked(true);
      setSupportFailure(
        message
          ? { code: "APPROVAL_PASSKEY_UNSUPPORTED", message }
          : null,
      );
    }, 0);

    return () => window.clearTimeout(timeout);
  }, []);

  useEffect(() => {
    if (!challenge) {
      return;
    }

    const remaining = Date.parse(challenge.expires_at) - Date.now();
    const timeout = window.setTimeout(
      () => setChallengeExpired(true),
      Math.max(0, remaining),
    );
    return () => window.clearTimeout(timeout);
  }, [challenge]);

  const identityBusy = identityPhase === "creating" || identityPhase === "loading";
  const registrationBusy = registrationPhase !== "idle";
  const approvalBusy =
    approvalPhase === "preparing" ||
    approvalPhase === "browser" ||
    approvalPhase === "verifying";
  const canUsePasskeys = supportChecked && supportFailure === null;

  const acceptIdentity = (candidate: ApprovalIdentity) => {
    if (candidate.subject_ref !== policySubjectRef) {
      throw new ApprovalRequestFailure(
        "APPROVAL_IDENTITY_SUBJECT_MISMATCH",
        "This approval identity belongs to a different policy subject.",
      );
    }

    setIdentity(candidate);
    setPasskeyRegistered(identityHasPasskey(candidate));
    setIdentityPhase("resolved");
    setChallenge(null);
    setAuthorization(null);
    setApprovalPhase("idle");
    window.setTimeout(() => identityHeadingRef.current?.focus(), 0);
  };

  const createIdentity = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedName = displayName.trim();
    if (!normalizedName) {
      setIdentityFailure({
        code: "APPROVAL_IDENTITY_INVALID",
        message: "Enter a display name for the approval identity.",
      });
      return;
    }

    setIdentityFailure(null);
    setIdentityPhase("creating");
    try {
      const body = await requestJson(
        `${apiBaseEndpoint}/approval-identities`,
        "POST",
        { subject_ref: policySubjectRef, display_name: normalizedName },
      );
      const created = parseIdentity(body);
      if (!created) {
        throw new ApprovalRequestFailure(
          "APPROVAL_RESPONSE_INVALID",
          "The server returned an unexpected approval identity.",
        );
      }
      acceptIdentity(created);
    } catch (error: unknown) {
      setIdentityPhase("idle");
      setIdentityFailure(failureFrom(error, "approval"));
    }
  };

  const loadIdentity = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const identityId = existingIdentity.trim();
    if (!identityId) {
      setIdentityFailure({
        code: "APPROVAL_IDENTITY_INVALID",
        message: "Enter an approval identity ID.",
      });
      return;
    }

    setIdentityFailure(null);
    setIdentityPhase("loading");
    try {
      const body = await requestJson(
        `${apiBaseEndpoint}/approval-identities/${encodeURIComponent(identityId)}`,
        "GET",
      );
      const loaded = parseIdentity(body);
      if (!loaded || loaded.id !== identityId) {
        throw new ApprovalRequestFailure(
          "APPROVAL_RESPONSE_INVALID",
          "The server returned an unexpected approval identity.",
        );
      }
      acceptIdentity(loaded);
    } catch (error: unknown) {
      setIdentityPhase("idle");
      setIdentityFailure(failureFrom(error, "approval"));
    }
  };

  const registerPasskey = async () => {
    if (!identity || !canUsePasskeys || registrationBusy) {
      return;
    }

    setRegistrationFailure(null);
    setRegistrationPhase("options");
    try {
      const body = await requestJson(
        `${apiBaseEndpoint}/approval-identities/${encodeURIComponent(identity.id)}/passkeys/options`,
        "POST",
        {},
      );
      const options = parseCeremonyOptions(body);
      if (!options) {
        throw new ApprovalRequestFailure(
          "APPROVAL_RESPONSE_INVALID",
          "The server returned unexpected passkey registration options.",
        );
      }

      setRegistrationPhase("browser");
      const credential = await createPasskeyCredential(options.public_key);
      setRegistrationPhase("verifying");
      const verified = await requestJson(
        `${apiBaseEndpoint}/approval-identities/${encodeURIComponent(identity.id)}/passkeys/verify`,
        "POST",
        { challenge_id: options.challenge_id, credential },
      );

      const registeredCredential = verified.credential;
      if (
        verified.status !== "registered" ||
        !isRecord(registeredCredential) ||
        !isNonEmptyString(registeredCredential.id)
      ) {
        throw new ApprovalRequestFailure(
          "APPROVAL_RESPONSE_INVALID",
          "The server returned an unexpected passkey registration result.",
        );
      }

      setPasskeyRegistered(true);
      setRegistrationPhase("idle");
      window.setTimeout(() => passkeyStatusRef.current?.focus(), 0);
    } catch (error: unknown) {
      setRegistrationPhase("idle");
      setRegistrationFailure(failureFrom(error, "registration"));
    }
  };

  const prepareReview = async () => {
    if (!identity || !passkeyRegistered || !canUsePasskeys || approvalBusy) {
      return;
    }

    setApprovalFailure(null);
    setChallenge(null);
    setChallengeExpired(false);
    setAuthorization(null);
    setApprovalPhase("preparing");
    try {
      const body = await requestJson(
        `${apiBaseEndpoint}/approval-challenges`,
        "POST",
        {
          evaluation_id: evaluationId,
          approval_identity_id: identity.id,
        },
      );
      const prepared = parseApprovalChallenge(body);
      if (
        !prepared ||
        prepared.review.evaluation_id !== evaluationId ||
        prepared.review.approval_identity_id !== identity.id ||
        prepared.review.subject_ref !== policySubjectRef
      ) {
        throw new ApprovalRequestFailure(
          "APPROVAL_RESPONSE_INVALID",
          "The server returned an unexpected trusted review payload.",
        );
      }

      setChallenge(prepared);
      setApprovalPhase("ready");
    } catch (error: unknown) {
      setApprovalPhase("idle");
      setApprovalFailure(failureFrom(error, "approval"));
    }
  };

  const approveWithPasskey = async () => {
    if (!challenge || !identity || !canUsePasskeys || approvalBusy) {
      return;
    }

    if (challengeExpired) {
      setApprovalFailure({
        code: "APPROVAL_CHALLENGE_EXPIRED",
        message: "This approval challenge expired. Prepare a new trusted review.",
      });
      return;
    }

    setApprovalFailure(null);
    setApprovalPhase("browser");
    try {
      const credential = await getPasskeyCredential(challenge.public_key);
      setApprovalPhase("verifying");

      let body: Record<string, unknown>;
      try {
        body = await requestJson(
          `${apiBaseEndpoint}/approval-challenges/${encodeURIComponent(challenge.challenge_id)}/verify`,
          "POST",
          { credential },
        );
      } catch (error: unknown) {
        setChallenge(null);
        throw error;
      }

      const verified = parseAuthorization(body);
      if (
        !verified ||
        verified.evaluation_id !== evaluationId ||
        verified.review_hash !== challenge.review_hash ||
        verified.subject_ref !== policySubjectRef
      ) {
        setChallenge(null);
        throw new ApprovalRequestFailure(
          "APPROVAL_RESPONSE_INVALID",
          "The server returned an unexpected purchase authorization.",
        );
      }

      setAuthorization(verified);
      setChallenge(null);
      setApprovalPhase("authorized");
    } catch (error: unknown) {
      if (error instanceof WebAuthnBrowserFailure) {
        setApprovalPhase("ready");
      } else {
        setApprovalPhase("idle");
      }
      setApprovalFailure(failureFrom(error, "approval"));
    }
  };

  const resetIdentity = () => {
    setIdentity(null);
    setIdentityPhase("idle");
    setIdentityFailure(null);
    setPasskeyRegistered(false);
    setRegistrationPhase("idle");
    setRegistrationFailure(null);
    setChallenge(null);
    setAuthorization(null);
    setApprovalPhase("idle");
    setApprovalFailure(null);
  };

  return (
    <section className="mt-4 rounded-2xl border border-amber-300/20 bg-amber-300/[0.025] p-4">
      <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-amber-200/75">
        Trusted human approval
      </p>
      <h6 className="mt-2 text-base font-semibold text-white">
        READY FOR HUMAN APPROVAL
      </h6>
      <p className="mt-2 text-xs leading-5 text-slate-300">
        Policy ALLOW is not purchase authorization. A registered human passkey
        must confirm the exact server review.
      </p>
      <p className="mt-2 text-xs font-semibold leading-5 text-amber-100">
        Your AI agent cannot perform this step.
      </p>

      {!supportChecked ? (
        <p role="status" className="mt-3 text-xs text-slate-400">
          Checking passkey support…
        </p>
      ) : null}
      {supportFailure ? <FailureNotice failure={supportFailure} /> : null}

      {!identity ? (
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <form
            onSubmit={createIdentity}
            aria-busy={identityPhase === "creating"}
            className="rounded-xl border border-white/[0.08] bg-black/15 p-3"
          >
            <fieldset disabled={identityBusy}>
              <legend className="text-xs font-semibold text-slate-200">
                Create approval identity
              </legend>
              <p className="mt-2 text-[11px] leading-5 text-slate-500">
                Subject is fixed to this policy: {policySubjectRef}
              </p>
              <label
                htmlFor={displayNameId}
                className="mt-3 block text-xs font-medium text-slate-300"
              >
                Display name
              </label>
              <input
                id={displayNameId}
                value={displayName}
                required
                autoComplete="name"
                onChange={(event) => {
                  setDisplayName(event.target.value);
                  setIdentityFailure(null);
                }}
                className="mt-2 w-full rounded-lg border border-white/10 bg-black/25 px-3 py-2 text-sm text-slate-200 outline-none transition focus:border-amber-300/35 focus:ring-2 focus:ring-amber-300/10 disabled:cursor-wait disabled:opacity-60"
              />
              <button
                type="submit"
                className="mt-3 w-full rounded-lg border border-amber-300/20 bg-amber-300/[0.07] px-3 py-2 text-xs font-medium text-amber-100 transition hover:border-amber-300/35 hover:bg-amber-300/[0.1] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-200 disabled:cursor-wait disabled:opacity-60"
              >
                {identityPhase === "creating"
                  ? "Creating identity…"
                  : "Create approval identity"}
              </button>
            </fieldset>
          </form>

          <form
            onSubmit={loadIdentity}
            aria-busy={identityPhase === "loading"}
            className="rounded-xl border border-white/[0.08] bg-black/15 p-3"
          >
            <fieldset disabled={identityBusy}>
              <legend className="text-xs font-semibold text-slate-200">
                Use existing identity
              </legend>
              <p className="mt-2 text-[11px] leading-5 text-slate-500">
                The server must confirm that its subject matches this policy.
              </p>
              <label
                htmlFor={existingIdentityId}
                className="mt-3 block text-xs font-medium text-slate-300"
              >
                Approval identity ID
              </label>
              <input
                id={existingIdentityId}
                value={existingIdentity}
                required
                autoComplete="off"
                placeholder="aid_…"
                onChange={(event) => {
                  setExistingIdentity(event.target.value);
                  setIdentityFailure(null);
                }}
                className="mt-2 w-full rounded-lg border border-white/10 bg-black/25 px-3 py-2 font-mono text-sm text-slate-200 outline-none transition placeholder:text-slate-700 focus:border-amber-300/35 focus:ring-2 focus:ring-amber-300/10 disabled:cursor-wait disabled:opacity-60"
              />
              <button
                type="submit"
                className="mt-3 w-full rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-xs font-medium text-slate-200 transition hover:border-amber-300/25 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-200 disabled:cursor-wait disabled:opacity-60"
              >
                {identityPhase === "loading" ? "Loading identity…" : "Load identity"}
              </button>
            </fieldset>
          </form>
        </div>
      ) : (
        <div className="mt-4 rounded-xl border border-white/[0.08] bg-black/15 p-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h6
                ref={identityHeadingRef}
                tabIndex={-1}
                className="text-sm font-semibold text-white focus:outline-none"
              >
                {identity.display_name}
              </h6>
              <p className="mt-1 break-all font-mono text-[10px] text-slate-500">
                {identity.id}
              </p>
              <p className="mt-1 break-all text-[11px] text-slate-400">
                Subject: {identity.subject_ref}
              </p>
            </div>
            <span className="rounded-full border border-white/10 px-2.5 py-1 text-[10px] font-medium uppercase tracking-[0.1em] text-slate-300">
              {identity.status}
            </span>
          </div>

          {identity.status === "disabled" ? (
            <FailureNotice
              failure={{
                code: "APPROVAL_IDENTITY_DISABLED",
                message: "This approval identity is disabled and cannot approve purchases.",
              }}
            />
          ) : null}

          {passkeyRegistered ? (
            <div
              ref={passkeyStatusRef}
              tabIndex={-1}
              role="status"
              className="mt-3 rounded-lg border border-emerald-300/15 bg-emerald-300/[0.05] px-3 py-2.5 focus:outline-none"
            >
              <p className="text-xs font-semibold text-emerald-100">
                Passkey registered
              </p>
              <p className="mt-1 text-[11px] text-emerald-100/70">
                Credential available for approvals
              </p>
            </div>
          ) : identity.status === "active" ? (
            <button
              type="button"
              disabled={!canUsePasskeys || registrationBusy || approvalBusy}
              onClick={registerPasskey}
              className="mt-3 w-full rounded-lg border border-cyan-300/20 bg-cyan-300/[0.07] px-3 py-2.5 text-xs font-medium text-cyan-100 transition hover:border-cyan-300/35 hover:bg-cyan-300/[0.1] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {registrationPhase === "options"
                ? "Requesting registration challenge…"
                : registrationPhase === "browser"
                  ? "Waiting for passkey registration…"
                  : registrationPhase === "verifying"
                    ? "Verifying passkey…"
                    : "Register Passkey"}
            </button>
          ) : null}

          {registrationFailure ? (
            <FailureNotice failure={registrationFailure} />
          ) : null}

          <button
            type="button"
            disabled={registrationBusy || approvalBusy}
            onClick={resetIdentity}
            className="mt-3 w-full rounded-lg border border-white/10 px-3 py-2 text-xs font-medium text-slate-400 transition hover:border-white/20 hover:text-slate-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-200 disabled:cursor-wait disabled:opacity-50"
          >
            Use another identity
          </button>
        </div>
      )}

      {identityFailure ? <FailureNotice failure={identityFailure} /> : null}

      {identity?.status === "active" && passkeyRegistered && !authorization ? (
        <div className="mt-4 border-t border-white/[0.08] pt-4">
          {!challenge ? (
            <button
              type="button"
              disabled={!canUsePasskeys || approvalBusy}
              onClick={prepareReview}
              className="w-full rounded-xl border border-amber-300/25 bg-amber-300/[0.08] px-4 py-2.5 text-sm font-medium text-amber-100 transition hover:border-amber-300/40 hover:bg-amber-300/[0.12] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-200 disabled:cursor-wait disabled:opacity-50"
            >
              {approvalPhase === "preparing"
                ? "Preparing trusted review…"
                : "Prepare trusted review"}
            </button>
          ) : (
            <>
              <ReviewPanel challenge={challenge} />
              <button
                type="button"
                disabled={approvalBusy || challengeExpired}
                onClick={approveWithPasskey}
                className="mt-3 w-full rounded-xl border border-emerald-300/25 bg-emerald-300/[0.08] px-4 py-3 text-sm font-semibold text-emerald-100 transition hover:border-emerald-300/40 hover:bg-emerald-300/[0.13] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-200 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {approvalPhase === "browser"
                  ? "Waiting for human verification…"
                  : approvalPhase === "verifying"
                    ? "Verifying approval…"
                    : "Approve with Passkey"}
              </button>
              {challengeExpired ? (
                <>
                  <FailureNotice
                    failure={{
                      code: "APPROVAL_CHALLENGE_EXPIRED",
                      message:
                        "This approval challenge expired before human verification.",
                    }}
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setChallenge(null);
                      setApprovalPhase("idle");
                      setApprovalFailure(null);
                    }}
                    className="mt-2 w-full rounded-lg border border-white/10 px-3 py-2 text-xs font-medium text-slate-300 transition hover:border-white/20 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-200"
                  >
                    Prepare a new challenge
                  </button>
                </>
              ) : null}
            </>
          )}
        </div>
      ) : null}

      {approvalPhase === "browser" || approvalPhase === "verifying" ? (
        <p role="status" className="mt-3 text-xs leading-5 text-slate-400">
          {approvalPhase === "browser"
            ? "Complete the passkey prompt on this device."
            : "The server is verifying human presence and rechecking the approval context."}
        </p>
      ) : null}

      {approvalFailure ? <FailureNotice failure={approvalFailure} /> : null}
      {authorization ? <AuthorizationPanel authorization={authorization} /> : null}
    </section>
  );
}
