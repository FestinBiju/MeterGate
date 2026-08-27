"use client";

import { useEffect, useRef, useState } from "react";

import { useAccountSession } from "@/components/account-session";
import { StandardCheckout } from "@/components/standard-checkout";
import { ApiRequestFailure } from "@/lib/api-client";
import {
  getPasskeyCredential,
  passkeySupportError,
  WebAuthnBrowserFailure,
} from "@/lib/webauthn";

type JsonPrimitive = boolean | null | number | string;
type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

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
  merchantId: string;
  merchantName: string;
  serviceId: string;
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

const integerFormatter = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 0,
});
const dateTimeFormatter = new Intl.DateTimeFormat("en-IN", {
  dateStyle: "medium",
  timeStyle: "medium",
});

function authorizationStorageKey(evaluationId: string): string {
  return `metergate:purchase-authorization:v1:${evaluationId}`;
}

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

function entityId(
  value: Record<string, unknown>,
  field: "merchant" | "service",
): string | null {
  const nested = value[field];
  return isRecord(nested) && isNonEmptyString(nested.id) ? nested.id : null;
}

function parseAuthorization(value: unknown): Authorization | null {
  if (!isRecord(value)) {
    return null;
  }

  const merchantId = entityId(value, "merchant");
  const merchantName = entityName(value, "merchant");
  const serviceId = entityId(value, "service");
  const serviceName = entityName(value, "service");
  if (
    !isNonEmptyString(value.id) ||
    (value.state !== "active" && value.state !== "expired") ||
    !isNonEmptyString(value.subject_ref) ||
    !merchantId ||
    !merchantName ||
    !serviceId ||
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
    merchantId,
    merchantName,
    serviceId,
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

function failureFrom(error: unknown): DisplayFailure {
  if (error instanceof ApiRequestFailure) {
    return { code: error.code, message: error.message };
  }

  if (error instanceof ApprovalRequestFailure) {
    return { code: error.code, message: error.message };
  }

  if (error instanceof WebAuthnBrowserFailure) {
    const code =
      error.kind === "cancelled"
        ? "APPROVAL_CANCELLED"
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
        This authorization has not moved money. Continue below to create one
        server-bound Razorpay Test Mode transaction.
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
  const { state, requestAuthenticated } = useAccountSession();
  const [supportFailure, setSupportFailure] = useState<DisplayFailure | null>(
    null,
  );
  const [supportChecked, setSupportChecked] = useState(false);
  const [approvalPhase, setApprovalPhase] = useState<ApprovalPhase>("idle");
  const [approvalFailure, setApprovalFailure] =
    useState<DisplayFailure | null>(null);
  const [challenge, setChallenge] = useState<ApprovalChallenge | null>(null);
  const [challengeExpired, setChallengeExpired] = useState(false);
  const [authorization, setAuthorization] = useState<Authorization | null>(null);
  const restoredAuthorizationKey = useRef<string | null>(null);

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

  const approvalBusy =
    approvalPhase === "preparing" ||
    approvalPhase === "browser" ||
    approvalPhase === "verifying";
  const canUsePasskeys = supportChecked && supportFailure === null;
  const session = state.kind === "authenticated" ? state.session : null;
  const accountMatchesPolicy = session?.account.id === policySubjectRef;

  useEffect(() => {
    if (!session || !accountMatchesPolicy || authorization) {
      return;
    }

    const storageKey = authorizationStorageKey(evaluationId);
    if (restoredAuthorizationKey.current === storageKey) {
      return;
    }
    restoredAuthorizationKey.current = storageKey;

    const authorizationId = window.sessionStorage.getItem(storageKey);
    if (!authorizationId) {
      return;
    }

    let active = true;
    void requestAuthenticated(
      `${apiBaseEndpoint}/authorizations/${encodeURIComponent(authorizationId)}`,
      { method: "GET" },
    )
      .then((body) => {
        const restored = parseAuthorization(body);
        if (
          !restored ||
          restored.id !== authorizationId ||
          restored.evaluation_id !== evaluationId ||
          restored.subject_ref !== session.account.id
        ) {
          throw new ApprovalRequestFailure(
            "APPROVAL_RESPONSE_INVALID",
            "The server returned an unexpected purchase authorization.",
          );
        }
        if (active) {
          setAuthorization(restored);
          setApprovalPhase("authorized");
        }
      })
      .catch(() => {
        window.sessionStorage.removeItem(storageKey);
        if (active) {
          setApprovalPhase("idle");
        }
      });

    return () => {
      active = false;
    };
  }, [
    accountMatchesPolicy,
    apiBaseEndpoint,
    authorization,
    evaluationId,
    requestAuthenticated,
    session,
  ]);

  const prepareReview = async () => {
    if (!session || !accountMatchesPolicy || !canUsePasskeys || approvalBusy) {
      return;
    }

    setApprovalFailure(null);
    setChallenge(null);
    setChallengeExpired(false);
    setAuthorization(null);
    setApprovalPhase("preparing");
    try {
      const body = await requestAuthenticated(
        `${apiBaseEndpoint}/approval-challenges`,
        { method: "POST", body: { evaluation_id: evaluationId } },
      );
      const prepared = parseApprovalChallenge(body);
      if (
        !prepared ||
        prepared.review.evaluation_id !== evaluationId ||
        prepared.review.approval_identity_id !== session.approval_identity.id ||
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
      setApprovalFailure(failureFrom(error));
    }
  };

  const approveWithPasskey = async () => {
    if (
      !challenge ||
      !session ||
      !accountMatchesPolicy ||
      !canUsePasskeys ||
      approvalBusy
    ) {
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
        body = await requestAuthenticated(
          `${apiBaseEndpoint}/approval-challenges/${encodeURIComponent(challenge.challenge_id)}/verify`,
          { method: "POST", body: { credential } },
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
      window.sessionStorage.setItem(
        authorizationStorageKey(evaluationId),
        verified.id,
      );
      setChallenge(null);
      setApprovalPhase("authorized");
    } catch (error: unknown) {
      if (error instanceof WebAuthnBrowserFailure) {
        setApprovalPhase("ready");
      } else {
        setApprovalPhase("idle");
      }
      setApprovalFailure(failureFrom(error));
    }
  };

  if (!session) {
    return (
      <div className="mt-4 rounded-xl border border-violet-300/15 bg-violet-300/[0.035] px-4 py-4">
        <p className="text-sm font-semibold text-violet-100">
          Sign in to continue
        </p>
        <p className="mt-2 text-xs leading-5 text-slate-400">
          Purchase approval requires the passkey-authenticated owner of this
          buyer policy.
        </p>
        <a
          href="#buyer-account"
          className="mt-3 inline-flex rounded-lg border border-violet-300/20 px-3 py-2 text-xs font-medium text-violet-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-200"
        >
          Sign in to continue
        </a>
      </div>
    );
  }

  if (!accountMatchesPolicy) {
    return (
      <FailureNotice
        failure={{
          code: "AUTH_RESOURCE_OWNERSHIP_MISMATCH",
          message:
            "This policy does not belong to the currently authenticated account.",
        }}
      />
    );
  }

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

      <div className="mt-4 rounded-xl border border-emerald-300/15 bg-emerald-300/[0.04] p-3">
        <p className="text-xs font-semibold text-emerald-100">
          Account-bound approval identity ready
        </p>
        <p className="mt-1 break-all text-[11px] leading-5 text-slate-400">
          Signed in as {session.account.display_name} ·{" "}
          <span className="font-mono">{session.account.id}</span>
        </p>
        <p className="mt-1 text-[11px] text-emerald-100/70">
          {session.approval_identity.credential_count} registered passkey
          {session.approval_identity.credential_count === 1 ? "" : "s"} can
          verify this approval. Identity selection and enrollment are not
          accepted from this purchase flow.
        </p>
      </div>

      {!authorization ? (
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
      {authorization ? (
        <>
          <AuthorizationPanel authorization={authorization} />
          <StandardCheckout
            apiBaseEndpoint={apiBaseEndpoint}
            authorization={authorization}
          />
        </>
      ) : null}
    </section>
  );
}
