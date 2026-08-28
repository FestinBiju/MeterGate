"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useAccountSession } from "@/components/account-session";
import {
  ApiRequestFailure,
  requestProtectedResourceJson,
  type JsonValue,
} from "@/lib/api-client";

type DisplayFailure = {
  code: string;
  message: string;
};

export type CommerceOutcome =
  | "payment_pending"
  | "paid"
  | "fulfillment_pending"
  | "fulfilled"
  | "compensation_pending"
  | "manual_review"
  | "refunded";

export type CompensationSummary = {
  compensation_id: string;
  decision_state:
    | "pending"
    | "approved"
    | "rejected"
    | "executing"
    | "completed"
    | "manual_review";
  recommended_action: string;
  approved_refund_amount: number | null;
  decision_reason_code: string;
  failure_code: string;
  fulfillment_execution_id: string;
};

export type RefundSummary = {
  refund_id: string;
  provider_payment_id: string;
  provider_refund_id: string | null;
  amount: number;
  currency: string;
  state:
    | "refund_pending"
    | "refund_processing"
    | "refunded"
    | "refund_failed"
    | "refund_uncertain"
    | "reconciliation_required";
  provider_status: string | null;
  reconciliation_reason_code: string | null;
};

type EntitlementParty = {
  id: string;
  slug: string;
  name: string;
};

type PaidEntitlement = {
  entitlement_id: string;
  transaction_id: string;
  merchant: EntitlementParty;
  service: EntitlementParty;
  input: JsonValue;
  input_hash: string;
  amount: number;
  currency: string;
  purchase_type: "one_time";
  maximum_executions: 1;
  issued_at: string;
  expires_at: string;
  state: "active" | "expired";
  entitlement_version: string;
  entitlement_hash: string;
};

type TimelineEvent = {
  event_type: string;
  reason_code: string;
  occurred_at: string;
};

type EntitlementLookup =
  | {
      transaction_id: string;
      state: "pending" | "blocked";
      reason_code: string;
      entitlement: null;
      timeline: TimelineEvent[];
    }
  | {
      transaction_id: string;
      state: "ready";
      reason_code: string;
      entitlement: PaidEntitlement;
      timeline: TimelineEvent[];
    };

type CapabilityMetadata = {
  entitlement_id: string;
  expires_at: string;
  maximum_executions: 1;
};

type ResourceExecution = {
  execution_id: string;
  entitlement_id: string;
  result_content_type: string;
  result: JsonValue;
  result_hash: string;
  result_size_bytes: number;
  replayed_result: boolean;
  completed_at: string;
};

type AccessOperation = "idle" | "capability" | "challenge" | "execute" | "replay";

const transactionIdPattern = /^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const entitlementIdPattern = /^ent_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const executionIdPattern = /^ful_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const merchantIdPattern = /^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const serviceIdPattern = /^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const slugPattern = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const hashPattern = /^sha256:[0-9a-f]{64}$/;
const compactJwtPattern =
  /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;
const integerFormatter = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 0,
});
const timestampFormatter = new Intl.DateTimeFormat("en", {
  dateStyle: "medium",
  timeStyle: "medium",
});
const recoveryPollableOutcomes = new Set<CommerceOutcome>([
  "paid",
  "fulfillment_pending",
  "compensation_pending",
  "manual_review",
]);
const capabilityQuarantineCodes = new Set([
  "ENTITLEMENT_COMPENSATION_QUARANTINED",
  "CAPABILITY_COMPENSATION_QUARANTINED",
  "FULFILLMENT_COMPENSATION_REQUIRED",
  "REFUND_RECONCILIATION_REQUIRED",
  "PAYMENT_REFUND_EVIDENCE_DETECTED",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function safeText(value: unknown, maximumLength: number): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return normalized ? normalized.slice(0, maximumLength) : null;
}

function isTimestamp(value: unknown): value is string {
  return isNonEmptyString(value) && Number.isFinite(Date.parse(value));
}

function isJsonValue(value: unknown, depth = 0): value is JsonValue {
  if (depth > 32) {
    return false;
  }
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return true;
  }
  if (typeof value === "number") {
    return Number.isFinite(value);
  }
  if (Array.isArray(value)) {
    return (
      value.length <= 10_000 &&
      value.every((item) => isJsonValue(item, depth + 1))
    );
  }
  if (!isRecord(value) || Object.keys(value).length > 10_000) {
    return false;
  }
  return Object.values(value).every((item) => isJsonValue(item, depth + 1));
}

function jsonValuesEqual(left: JsonValue, right: JsonValue): boolean {
  if (left === right) {
    return true;
  }
  if (Array.isArray(left) && Array.isArray(right)) {
    return (
      left.length === right.length &&
      left.every((item, index) => jsonValuesEqual(item, right[index]))
    );
  }
  if (isRecord(left) && isRecord(right)) {
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    return (
      leftKeys.length === rightKeys.length &&
      leftKeys.every(
        (key, index) =>
          key === rightKeys[index] &&
          isJsonValue(left[key]) &&
          isJsonValue(right[key]) &&
          jsonValuesEqual(left[key], right[key]),
      )
    );
  }
  return false;
}

function parseParty(
  value: unknown,
  idPattern: RegExp,
): EntitlementParty | null {
  if (!isRecord(value)) {
    return null;
  }
  const name = safeText(value.name, 200);
  if (
    !isNonEmptyString(value.id) ||
    !idPattern.test(value.id) ||
    !isNonEmptyString(value.slug) ||
    value.slug.length > 100 ||
    !slugPattern.test(value.slug) ||
    !name
  ) {
    return null;
  }
  return { id: value.id, slug: value.slug, name };
}

function parseEntitlement(
  value: unknown,
  transactionId: string,
): PaidEntitlement | null {
  if (!isRecord(value)) {
    return null;
  }
  const merchant = parseParty(value.merchant, merchantIdPattern);
  const service = parseParty(value.service, serviceIdPattern);
  if (
    !isNonEmptyString(value.entitlement_id) ||
    !entitlementIdPattern.test(value.entitlement_id) ||
    value.transaction_id !== transactionId ||
    !merchant ||
    !service ||
    !isJsonValue(value.input) ||
    !isNonEmptyString(value.input_hash) ||
    !hashPattern.test(value.input_hash) ||
    typeof value.amount !== "number" ||
    !Number.isSafeInteger(value.amount) ||
    value.amount < 0 ||
    !isNonEmptyString(value.currency) ||
    !/^[A-Z]{3}$/.test(value.currency) ||
    value.purchase_type !== "one_time" ||
    value.maximum_executions !== 1 ||
    !isTimestamp(value.issued_at) ||
    !isTimestamp(value.expires_at) ||
    Date.parse(value.expires_at) <= Date.parse(value.issued_at) ||
    (value.state !== "active" && value.state !== "expired") ||
    !isNonEmptyString(value.entitlement_version) ||
    value.entitlement_version.length > 20 ||
    !isNonEmptyString(value.entitlement_hash) ||
    !hashPattern.test(value.entitlement_hash)
  ) {
    return null;
  }
  return {
    entitlement_id: value.entitlement_id,
    transaction_id: transactionId,
    merchant,
    service,
    input: value.input,
    input_hash: value.input_hash,
    amount: value.amount,
    currency: value.currency,
    purchase_type: "one_time",
    maximum_executions: 1,
    issued_at: value.issued_at,
    expires_at: value.expires_at,
    state: value.state,
    entitlement_version: value.entitlement_version,
    entitlement_hash: value.entitlement_hash,
  };
}

function parseEntitlementLookup(
  value: unknown,
  transactionId: string,
): EntitlementLookup | null {
  if (
    !isRecord(value) ||
    value.transaction_id !== transactionId ||
    !isNonEmptyString(value.reason_code) ||
    value.reason_code.length > 100
  ) {
    return null;
  }
  if (!Array.isArray(value.timeline) || value.timeline.length > 1_000) {
    return null;
  }
  const timeline: TimelineEvent[] = [];
  for (const item of value.timeline) {
    if (
      !isRecord(item) ||
      !isNonEmptyString(item.event_type) ||
      item.event_type.length > 100 ||
      !isNonEmptyString(item.reason_code) ||
      item.reason_code.length > 100 ||
      !isTimestamp(item.occurred_at)
    ) {
      return null;
    }
    timeline.push({
      event_type: item.event_type,
      reason_code: item.reason_code,
      occurred_at: item.occurred_at,
    });
  }
  if (
    (value.state === "pending" || value.state === "blocked") &&
    value.entitlement === null
  ) {
    return {
      transaction_id: transactionId,
      state: value.state,
      reason_code: value.reason_code,
      entitlement: null,
      timeline,
    };
  }
  if (value.state === "ready") {
    const entitlement = parseEntitlement(value.entitlement, transactionId);
    if (entitlement) {
      return {
        transaction_id: transactionId,
        state: "ready",
        reason_code: value.reason_code,
        entitlement,
        timeline,
      };
    }
  }
  return null;
}

function parseCapability(
  value: unknown,
  entitlementId: string,
): { metadata: CapabilityMetadata; token: string } | null {
  if (
    !isRecord(value) ||
    value.entitlement_id !== entitlementId ||
    !isNonEmptyString(value.token) ||
    value.token.length > 8_192 ||
    !compactJwtPattern.test(value.token) ||
    !isTimestamp(value.expires_at) ||
    Date.parse(value.expires_at) <= Date.now() ||
    value.maximum_executions !== 1
  ) {
    return null;
  }
  return {
    metadata: {
      entitlement_id: entitlementId,
      expires_at: value.expires_at,
      maximum_executions: 1,
    },
    token: value.token,
  };
}

function parsePaymentChallenge(
  value: Record<string, unknown>,
  entitlement: PaidEntitlement,
): Record<string, unknown> | null {
  const merchant = value.merchant;
  const service = value.service;
  const quote = value.quote;
  const access = value.access;
  if (
    value.type !== "metergate_payment_required" ||
    value.protocol !== "metergate/1" ||
    value.payment_provider !== "razorpay" ||
    !isRecord(merchant) ||
    merchant.id !== entitlement.merchant.id ||
    merchant.slug !== entitlement.merchant.slug ||
    !isRecord(service) ||
    service.id !== entitlement.service.id ||
    service.slug !== entitlement.service.slug ||
    !isRecord(quote) ||
    quote.endpoint !== "/api/v1/quotes" ||
    !isRecord(quote.request) ||
    quote.request.service_id !== entitlement.service.id ||
    !isJsonValue(quote.request.input) ||
    !jsonValuesEqual(quote.request.input, entitlement.input) ||
    !isRecord(access) ||
    access.scheme !== "Bearer" ||
    access.maximum_executions !== 1 ||
    !isJsonValue(value)
  ) {
    return null;
  }
  return value;
}

function parseResourceExecution(
  value: unknown,
  entitlementId: string,
): ResourceExecution | null {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.execution_id) ||
    !executionIdPattern.test(value.execution_id) ||
    value.entitlement_id !== entitlementId ||
    !isNonEmptyString(value.result_content_type) ||
    !value.result_content_type.toLowerCase().startsWith("application/json") ||
    !isJsonValue(value.result) ||
    !isNonEmptyString(value.result_hash) ||
    !hashPattern.test(value.result_hash) ||
    typeof value.result_size_bytes !== "number" ||
    !Number.isSafeInteger(value.result_size_bytes) ||
    value.result_size_bytes < 0 ||
    typeof value.replayed_result !== "boolean" ||
    !isTimestamp(value.completed_at)
  ) {
    return null;
  }
  return {
    execution_id: value.execution_id,
    entitlement_id: entitlementId,
    result_content_type: value.result_content_type,
    result: value.result,
    result_hash: value.result_hash,
    result_size_bytes: value.result_size_bytes,
    replayed_result: value.replayed_result,
    completed_at: value.completed_at,
  };
}

function failureFrom(error: unknown): DisplayFailure {
  if (error instanceof ApiRequestFailure) {
    return { code: error.code, message: error.message };
  }
  return {
    code: "PAID_RESOURCE_REQUEST_FAILED",
    message: "The paid-resource request could not be completed.",
  };
}

function isCapabilityQuarantineFailure(error: unknown): boolean {
  return (
    error instanceof ApiRequestFailure &&
    (capabilityQuarantineCodes.has(error.code) ||
      error.code.includes("COMPENSATION_QUARANTINED"))
  );
}

function formatAmount(amount: number, currency: string): string {
  if (currency === "INR") {
    const rupees = Math.trunc(amount / 100);
    const paise = amount % 100;
    return `₹${integerFormatter.format(rupees)}.${paise.toString().padStart(2, "0")}`;
  }
  return `${currency} ${integerFormatter.format(amount)} minor units`;
}

function formatTimestamp(value: string): string {
  return timestampFormatter.format(new Date(value));
}

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
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

export function PaidResourceAccess({
  apiBaseEndpoint,
  transactionId,
  commerceOutcome,
  compensationSummary,
  refundSummary,
}: {
  apiBaseEndpoint: string;
  transactionId: string;
  commerceOutcome: CommerceOutcome;
  compensationSummary: CompensationSummary | null;
  refundSummary: RefundSummary | null;
}) {
  const { requestAuthenticated } = useAccountSession();
  const [lookup, setLookup] = useState<EntitlementLookup | null>(null);
  const [lookupFailure, setLookupFailure] = useState<DisplayFailure | null>(null);
  const [refreshCycle, setRefreshCycle] = useState(0);
  const [operation, setOperation] = useState<AccessOperation>("idle");
  const [operationFailure, setOperationFailure] = useState<DisplayFailure | null>(null);
  const [activityMessage, setActivityMessage] = useState<string | null>(null);
  const [capability, setCapability] = useState<CapabilityMetadata | null>(null);
  const [paymentChallenge, setPaymentChallenge] = useState<Record<string, unknown> | null>(null);
  const [execution, setExecution] = useState<ResourceExecution | null>(null);
  const [quarantineObserved, setQuarantineObserved] = useState(false);
  const capabilityTokenRef = useRef<string | null>(null);
  const operationControllerRef = useRef<AbortController | null>(null);
  const transactionIdValid = transactionIdPattern.test(transactionId);
  const summaryRequiresQuarantine =
    compensationSummary !== null ||
    refundSummary !== null ||
    commerceOutcome === "compensation_pending" ||
    commerceOutcome === "manual_review" ||
    commerceOutcome === "refunded";
  const accessQuarantined = summaryRequiresQuarantine || quarantineObserved;

  useEffect(() => {
    return () => {
      capabilityTokenRef.current = null;
      operationControllerRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!summaryRequiresQuarantine) {
      return;
    }
    capabilityTokenRef.current = null;
    operationControllerRef.current?.abort();
  }, [summaryRequiresQuarantine]);

  useEffect(() => {
    if (!transactionIdValid) {
      return;
    }

    let active = true;
    let controller: AbortController | null = null;
    let timeout: number | null = null;
    let failureCount = 0;

    const schedule = (delay: number) => {
      timeout = window.setTimeout(() => void poll(), delay);
    };
    const poll = async () => {
      controller = new AbortController();
      try {
        const body = await requestAuthenticated(
          `${apiBaseEndpoint}/payment-transactions/${encodeURIComponent(transactionId)}/entitlement`,
          { method: "GET", signal: controller.signal },
        );
        const parsed = parseEntitlementLookup(body, transactionId);
        if (!parsed) {
          throw new ApiRequestFailure(
            "ENTITLEMENT_RESPONSE_INVALID",
            "The access service returned an unexpected entitlement response.",
          );
        }
        if (!active) {
          return;
        }
        setLookup(parsed);
        setLookupFailure(null);
        failureCount = 0;
        if (
          parsed.state === "pending" ||
          recoveryPollableOutcomes.has(commerceOutcome)
        ) {
          schedule(1_500);
        }
      } catch (error: unknown) {
        if (!active || controller.signal.aborted) {
          return;
        }
        failureCount += 1;
        setLookupFailure(failureFrom(error));
        schedule(Math.min(2_000 * 2 ** Math.min(failureCount - 1, 2), 8_000));
      }
    };

    void poll();
    return () => {
      active = false;
      controller?.abort();
      if (timeout !== null) {
        window.clearTimeout(timeout);
      }
    };
  }, [
    apiBaseEndpoint,
    commerceOutcome,
    refreshCycle,
    requestAuthenticated,
    transactionId,
    transactionIdValid,
  ]);

  useEffect(() => {
    if (!capability) {
      return;
    }
    const remaining = Date.parse(capability.expires_at) - Date.now();
    const timeout = window.setTimeout(() => {
      capabilityTokenRef.current = null;
      setCapability(null);
      setActivityMessage(
        "The short-lived capability expired. Generate a new one to retrieve or replay the result.",
      );
    }, Math.max(0, remaining));
    return () => window.clearTimeout(timeout);
  }, [capability]);

  const entitlement = lookup?.state === "ready" ? lookup.entitlement : null;

  const generateCapability = useCallback(async () => {
    if (
      !entitlement ||
      entitlement.state !== "active" ||
      operation !== "idle" ||
      accessQuarantined
    ) {
      return;
    }
    operationControllerRef.current?.abort();
    const controller = new AbortController();
    operationControllerRef.current = controller;
    setOperation("capability");
    setOperationFailure(null);
    setActivityMessage("MeterGate is issuing an exact-resource, exact-input capability.");
    try {
      const body = await requestAuthenticated(
        `${apiBaseEndpoint}/entitlements/${encodeURIComponent(entitlement.entitlement_id)}/capability`,
        { method: "POST", signal: controller.signal },
      );
      const parsed = parseCapability(body, entitlement.entitlement_id);
      if (!parsed) {
        throw new ApiRequestFailure(
          "CAPABILITY_RESPONSE_INVALID",
          "The access service returned an unexpected capability response.",
        );
      }
      capabilityTokenRef.current = parsed.token;
      setCapability(parsed.metadata);
      setActivityMessage(
        "Capability ready in volatile browser memory. The token is intentionally not displayed or stored.",
      );
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        if (isCapabilityQuarantineFailure(error)) {
          capabilityTokenRef.current = null;
          setCapability(null);
          setQuarantineObserved(true);
          setRefreshCycle((current) => current + 1);
        }
        setOperationFailure(failureFrom(error));
      }
    } finally {
      if (operationControllerRef.current === controller) {
        operationControllerRef.current = null;
      }
      if (!controller.signal.aborted) {
        setOperation("idle");
      }
    }
  }, [
    accessQuarantined,
    apiBaseEndpoint,
    entitlement,
    operation,
    requestAuthenticated,
  ]);

  const callProtectedResource = useCallback(
    async (kind: "challenge" | "execute" | "replay") => {
      if (!entitlement || operation !== "idle" || accessQuarantined) {
        return;
      }
      const token = kind === "challenge" ? undefined : capabilityTokenRef.current;
      if (kind !== "challenge" && !token) {
        setOperationFailure({
          code: "CAPABILITY_REQUIRED",
          message: "Generate a short-lived access capability before executing the resource.",
        });
        return;
      }

      operationControllerRef.current?.abort();
      const controller = new AbortController();
      operationControllerRef.current = controller;
      setOperation(kind);
      setOperationFailure(null);
      setActivityMessage(
        kind === "challenge"
          ? "Calling the protected resource without credentials to inspect its payment contract."
          : kind === "replay"
            ? "Requesting the completed result again without another merchant execution."
            : "Presenting the capability and atomically claiming the paid resource.",
      );
      try {
        const response = await requestProtectedResourceJson(
          `${apiBaseEndpoint}/resources/${encodeURIComponent(entitlement.merchant.slug)}/${encodeURIComponent(entitlement.service.slug)}/execute`,
          {
            body: entitlement.input,
            bearerToken: token ?? undefined,
            signal: controller.signal,
            timeoutMs: 45_000,
          },
        );
        if (kind === "challenge") {
          if (response.kind !== "payment_required") {
            throw new ApiRequestFailure(
              "RESOURCE_PROTECTION_MISSING",
              "The protected resource unexpectedly executed without a capability.",
              response.status,
            );
          }
          const parsed = parsePaymentChallenge(response.body, entitlement);
          if (!parsed) {
            throw new ApiRequestFailure(
              "PAYMENT_REQUIRED_RESPONSE_INVALID",
              "The HTTP 402 response did not match this paid resource.",
              402,
            );
          }
          setPaymentChallenge(parsed);
          setActivityMessage(
            "HTTP 402 captured. Its machine-readable quote request binds the same service and input.",
          );
          return;
        }
        if (response.kind === "payment_required") {
          throw new ApiRequestFailure(
            "CAPABILITY_NOT_ACCEPTED",
            "The protected resource did not accept the supplied capability.",
            402,
          );
        }
        const parsed = parseResourceExecution(
          response.body,
          entitlement.entitlement_id,
        );
        if (!parsed) {
          throw new ApiRequestFailure(
            "FULFILLMENT_RESPONSE_INVALID",
            "The protected resource returned an unexpected fulfillment response.",
            response.status,
          );
        }
        if (kind === "replay" && !parsed.replayed_result) {
          throw new ApiRequestFailure(
            "FULFILLMENT_REPLAY_INVALID",
            "The resource did not identify the retry as a stored-result replay.",
            response.status,
          );
        }
        setExecution(parsed);
        setActivityMessage(
          parsed.replayed_result
            ? "Stored result replayed. OrbitIntel was not executed as a new purchase."
            : "OrbitIntel fulfillment succeeded and MeterGate persisted the result.",
        );
      } catch (error: unknown) {
        if (!controller.signal.aborted) {
          if (isCapabilityQuarantineFailure(error)) {
            capabilityTokenRef.current = null;
            setCapability(null);
            setQuarantineObserved(true);
            setRefreshCycle((current) => current + 1);
          }
          setOperationFailure(failureFrom(error));
        }
      } finally {
        if (operationControllerRef.current === controller) {
          operationControllerRef.current = null;
        }
        if (!controller.signal.aborted) {
          setOperation("idle");
        }
      }
    },
    [accessQuarantined, apiBaseEndpoint, entitlement, operation],
  );

  const preparing = !lookup || lookup.state === "pending";
  const ready = lookup?.state === "ready";
  const blocked = lookup?.state === "blocked";
  const fulfillmentSucceeded =
    execution !== null ||
    lookup?.timeline.some((event) => event.event_type === "FULFILLMENT_SUCCEEDED") ===
      true;
  const fulfillmentRetryPending =
    !fulfillmentSucceeded &&
    lookup?.timeline.some(
      (event) => event.event_type === "FULFILLMENT_RETRY_SCHEDULED",
    ) === true;
  const refundConfirmed =
    commerceOutcome === "refunded" && refundSummary?.state === "refunded";
  const recoveryLabel = refundConfirmed
    ? "Refund completed"
    : commerceOutcome === "manual_review" ||
        refundSummary?.state === "refund_uncertain" ||
        refundSummary?.state === "reconciliation_required" ||
        refundSummary?.state === "refund_failed"
      ? "Manual review"
      : refundSummary
        ? "Refund processing"
        : "Compensation required";

  return (
    <section
      className={`mt-4 rounded-2xl border p-4 ${
        accessQuarantined
          ? "border-amber-300/20 bg-amber-300/[0.035]"
          : "border-emerald-300/20 bg-emerald-300/[0.035]"
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p
            className={`text-[10px] font-semibold uppercase tracking-[0.18em] ${
              accessQuarantined ? "text-amber-200/75" : "text-emerald-200/75"
            }`}
          >
            PAID RESOURCE ACCESS
          </p>
          <h6 className="mt-2 text-base font-semibold text-white">
            {accessQuarantined
              ? "Payment preserved · access quarantined"
              : fulfillmentSucceeded
                ? "Payment-bound OrbitIntel result"
                : fulfillmentRetryPending
                  ? "Entitlement ready · fulfillment retry pending"
                  : "Entitlement ready · fulfillment not yet completed"}
          </h6>
        </div>
        <span
          className={`rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.12em] ${
            accessQuarantined
              ? "border-amber-300/25 bg-amber-300/[0.08] text-amber-100"
              : "border-emerald-300/25 bg-emerald-300/[0.08] text-emerald-100"
          }`}
        >
          {accessQuarantined
            ? recoveryLabel
            : fulfillmentSucceeded
              ? "Result verified"
              : fulfillmentRetryPending
                ? "Retry pending"
                : "Entitlement verified"}
        </span>
      </div>

      <ol className="mt-4 grid gap-2 text-[10px] font-semibold uppercase tracking-[0.12em] sm:grid-cols-3">
        <li className="rounded-xl border border-emerald-300/25 bg-emerald-300/[0.08] px-3 py-2.5 text-emerald-100">
          <span className="block text-[9px] text-emerald-300/60">01 · Complete</span>
          Payment verified
        </li>
        <li
          className={`rounded-xl border px-3 py-2.5 ${
            preparing
              ? "border-cyan-300/25 bg-cyan-300/[0.08] text-cyan-100"
              : "border-emerald-300/25 bg-emerald-300/[0.08] text-emerald-100"
          }`}
        >
          <span className="block text-[9px] opacity-60">
            02 · {preparing ? "In progress" : "Complete"}
          </span>
          Preparing access
        </li>
        <li
          className={`rounded-xl border px-3 py-2.5 ${
            blocked || accessQuarantined
              ? "border-rose-300/25 bg-rose-300/[0.08] text-rose-100"
              : ready
                ? "border-emerald-300/25 bg-emerald-300/[0.08] text-emerald-100"
                : "border-white/[0.08] bg-black/20 text-slate-500"
          }`}
        >
          <span className="block text-[9px] opacity-60">
            03 · {accessQuarantined
              ? "Quarantined"
              : blocked
                ? "Blocked"
                : fulfillmentSucceeded
                  ? "Complete"
                  : fulfillmentRetryPending
                    ? "Retry pending"
                    : ready
                      ? "Ready"
                      : "Waiting"}
          </span>
          {accessQuarantined
            ? "Access stopped"
            : fulfillmentSucceeded
              ? "Result delivered"
              : fulfillmentRetryPending
                ? "Fulfillment retry"
                : "Awaiting execution"}
        </li>
      </ol>

      {lookup && lookup.timeline.length > 0 ? (
        <div className="mt-4 rounded-xl border border-white/[0.08] bg-black/15 px-3 py-3">
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400">
            Durable commerce timeline
          </p>
          <ol className="mt-3 grid gap-2">
            {lookup.timeline.slice(-50).map((event, index) => (
              <li
                key={`${event.occurred_at}:${event.event_type}:${index}`}
                className="flex flex-wrap items-baseline justify-between gap-2 border-l border-cyan-300/20 pl-3 text-[10px]"
              >
                <span className="font-mono text-cyan-100/80">
                  {event.event_type}
                </span>
                <span className="text-slate-600">
                  {formatTimestamp(event.occurred_at)} · {event.reason_code}
                </span>
              </li>
            ))}
          </ol>
        </div>
      ) : null}

      {fulfillmentRetryPending && !accessQuarantined ? (
        <div
          role="status"
          className="mt-4 rounded-xl border border-amber-300/20 bg-amber-300/[0.055] px-3 py-3"
        >
          <p className="text-sm font-semibold text-amber-100">
            FULFILLMENT RETRY PENDING
          </p>
          <p className="mt-2 text-xs leading-5 text-amber-50/80">
            OrbitIntel has not delivered a result yet. Retry with the same
            capability; MeterGate will reuse the existing logical fulfillment
            execution rather than create another purchase.
          </p>
          <p className="mt-2 text-[10px] leading-4 text-amber-100/65">
            Compensation and refund processing begin only if fulfillment becomes
            permanently failed or its configured attempts are exhausted.
          </p>
        </div>
      ) : null}

      {accessQuarantined ? (
        <div
          role="status"
          className="mt-4 rounded-xl border border-amber-300/20 bg-amber-300/[0.055] px-3 py-3"
        >
          <p className="text-sm font-semibold text-amber-100">
            ACCESS QUARANTINED · {recoveryLabel.toUpperCase()}
          </p>
          <p className="mt-2 text-xs leading-5 text-amber-50/80">
            The immutable entitlement and paid transaction remain in the audit trail,
            but MeterGate will not issue, execute, or replay a capability while
            compensation or refund evidence is active.
          </p>
          <p className="mt-2 text-[10px] leading-4 text-amber-100/65">
            TEST MODE — NO REAL MONEY MOVED. Refund completion is shown only after
            authoritative backend confirmation.
          </p>
        </div>
      ) : null}

      {preparing ? (
        <div role="status" className="mt-4 rounded-xl border border-cyan-300/15 bg-cyan-300/[0.04] px-3 py-3">
          <p className="text-sm font-semibold text-cyan-100">PREPARING ACCESS</p>
          <p className="mt-2 text-xs leading-5 text-slate-300">
            MeterGate is re-verifying payment evidence and issuing one immutable entitlement. This continues independently of the browser.
          </p>
          {lookup ? (
            <p className="mt-2 font-mono text-[10px] text-cyan-200/60">
              {lookup.reason_code}
            </p>
          ) : null}
        </div>
      ) : null}

      {blocked && lookup ? (
        <div role="alert" className="mt-4 rounded-xl border border-rose-300/20 bg-rose-300/[0.055] px-3 py-3">
          <p className="text-sm font-semibold text-rose-100">ACCESS BLOCKED</p>
          <p className="mt-2 text-xs leading-5 text-rose-50/80">
            MeterGate refused to release merchant value because the payment or entitlement gate needs attention.
          </p>
          <p className="mt-2 font-mono text-[10px] text-rose-200/70">
            {lookup.reason_code}
          </p>
          <button
            type="button"
            onClick={() => setRefreshCycle((current) => current + 1)}
            className="mt-3 rounded-lg border border-rose-200/20 px-3 py-2 text-xs font-medium text-rose-50 transition hover:border-rose-200/35 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-200"
          >
            Recheck access status
          </button>
        </div>
      ) : null}

      {!transactionIdValid ? (
        <FailureNotice
          failure={{
            code: "ENTITLEMENT_TRANSACTION_INVALID",
            message: "The verified payment transaction identifier is invalid.",
          }}
        />
      ) : lookupFailure ? (
        <FailureNotice failure={lookupFailure} />
      ) : null}

      {entitlement ? (
        <div className="mt-4 rounded-xl border border-emerald-300/20 bg-black/20 px-3 py-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <p className="text-sm font-semibold text-emerald-100">
                {accessQuarantined
                  ? "ENTITLEMENT PRESERVED"
                  : "ENTITLEMENT READY"}
              </p>
              <p className="mt-1 text-xs text-slate-400">
                {accessQuarantined
                  ? "Immutable evidence retained; paid access is not releasable"
                  : fulfillmentSucceeded
                    ? "Immutable entitlement and delivered result verified by MeterGate"
                    : fulfillmentRetryPending
                      ? "Immutable entitlement verified; fulfillment retry remains pending"
                      : "Immutable entitlement verified; no delivered result is claimed yet"}
              </p>
            </div>
            <span className={`rounded-full border px-2 py-1 text-[9px] font-semibold uppercase tracking-[0.12em] ${
              entitlement.state === "active"
                ? "border-emerald-300/20 text-emerald-200"
                : "border-amber-300/20 text-amber-200"
            }`}>
              {entitlement.state}
            </span>
          </div>
          <dl className="mt-3 grid gap-3 border-t border-white/[0.07] pt-3 text-[10px] sm:grid-cols-2">
            <div className="sm:col-span-2">
              <dt className="text-slate-600">Entitlement</dt>
              <dd className="mt-1 break-all font-mono text-slate-300">
                {entitlement.entitlement_id}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Merchant</dt>
              <dd className="mt-1 text-slate-300">
                {entitlement.merchant.name} · {entitlement.merchant.slug}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Service</dt>
              <dd className="mt-1 text-slate-300">
                {entitlement.service.name} · {entitlement.service.slug}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Purchase</dt>
              <dd className="mt-1 text-slate-300">
                {formatAmount(entitlement.amount, entitlement.currency)} · one time
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Maximum merchant executions</dt>
              <dd className="mt-1 text-slate-300">{entitlement.maximum_executions}</dd>
            </div>
            <div>
              <dt className="text-slate-600">Issued</dt>
              <dd className="mt-1 text-slate-300">{formatTimestamp(entitlement.issued_at)}</dd>
            </div>
            <div>
              <dt className="text-slate-600">Expires</dt>
              <dd className="mt-1 text-slate-300">{formatTimestamp(entitlement.expires_at)}</dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-slate-600">Exact input</dt>
              <dd className="mt-1 overflow-x-auto rounded-lg bg-black/30 p-2 font-mono text-slate-300">
                <pre>{prettyJson(entitlement.input)}</pre>
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-slate-600">Input hash</dt>
              <dd className="mt-1 break-all font-mono text-slate-400">
                {entitlement.input_hash}
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-slate-600">Entitlement hash · v{entitlement.entitlement_version}</dt>
              <dd className="mt-1 break-all font-mono text-slate-400">
                {entitlement.entitlement_hash}
              </dd>
            </div>
          </dl>

          <div className="mt-4 grid gap-2 sm:grid-cols-2">
            <button
              type="button"
              disabled={operation !== "idle" || accessQuarantined}
              onClick={() => void callProtectedResource("challenge")}
              className="rounded-xl border border-amber-300/20 bg-amber-300/[0.055] px-3 py-2.5 text-xs font-semibold text-amber-100 transition hover:border-amber-300/35 hover:bg-amber-300/[0.09] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-200 disabled:cursor-wait disabled:opacity-50"
            >
              {operation === "challenge"
                ? "Calling without capability…"
                : "1 · Call without capability (expect 402)"}
            </button>
            <button
              type="button"
              disabled={
                operation !== "idle" ||
                entitlement.state !== "active" ||
                accessQuarantined
              }
              onClick={() => void generateCapability()}
              className="rounded-xl border border-cyan-300/20 bg-cyan-300/[0.065] px-3 py-2.5 text-xs font-semibold text-cyan-100 transition hover:border-cyan-300/35 hover:bg-cyan-300/[0.1] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-wait disabled:opacity-50"
            >
              {operation === "capability"
                ? "Generating capability…"
                : capability
                  ? "2 · Regenerate access capability"
                  : "2 · Generate access capability"}
            </button>
            <button
              type="button"
              disabled={operation !== "idle" || !capability || accessQuarantined}
              onClick={() => void callProtectedResource("execute")}
              className="rounded-xl border border-emerald-300/20 bg-emerald-300/[0.065] px-3 py-2.5 text-xs font-semibold text-emerald-100 transition hover:border-emerald-300/35 hover:bg-emerald-300/[0.1] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-200 disabled:cursor-wait disabled:opacity-50"
            >
              {operation === "execute"
                ? "Executing protected resource…"
                : fulfillmentRetryPending
                  ? "3 · Retry fulfillment with capability"
                  : "3 · Execute with capability"}
            </button>
            <button
              type="button"
              disabled={
                operation !== "idle" ||
                !capability ||
                !execution ||
                accessQuarantined
              }
              onClick={() => void callProtectedResource("replay")}
              className="rounded-xl border border-violet-300/20 bg-violet-300/[0.055] px-3 py-2.5 text-xs font-semibold text-violet-100 transition hover:border-violet-300/35 hover:bg-violet-300/[0.09] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-200 disabled:cursor-wait disabled:opacity-50"
            >
              {operation === "replay"
                ? "Replaying stored result…"
                : "4 · Replay stored result"}
            </button>
          </div>

          {capability && !accessQuarantined ? (
            <div role="status" className="mt-3 rounded-xl border border-cyan-300/15 bg-cyan-300/[0.04] px-3 py-3">
              <p className="text-xs font-semibold text-cyan-100">Capability held in volatile memory</p>
              <p className="mt-1 text-[10px] leading-4 text-slate-400">
                Expires {formatTimestamp(capability.expires_at)} · maximum logical
                merchant executions {capability.maximum_executions}. Safe fulfillment
                retries reuse that logical execution. The bearer token is deliberately
                hidden and is never written to browser storage, URLs, or logs.
              </p>
            </div>
          ) : null}
        </div>
      ) : null}

      {activityMessage ? (
        <p role="status" className="mt-3 text-xs leading-5 text-slate-400">
          {activityMessage}
        </p>
      ) : null}
      {operationFailure ? <FailureNotice failure={operationFailure} /> : null}

      {paymentChallenge ? (
        <div className="mt-4 rounded-xl border border-amber-300/20 bg-amber-300/[0.045] px-3 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs font-semibold text-amber-100">
              HISTORICAL HTTP 402 · PAYMENT REQUIRED
            </p>
            <span className="font-mono text-[9px] text-amber-200/65">metergate/1</span>
          </div>
          <p className="mt-2 text-[10px] leading-4 text-slate-400">
            This diagnostic challenge was returned before any capability was
            presented. It is retained for protocol inspection and is not the current
            fulfillment response.
          </p>
          <pre className="mt-3 max-h-80 overflow-auto rounded-lg bg-black/35 p-3 text-[10px] leading-4 text-amber-50/80">
            {prettyJson(paymentChallenge)}
          </pre>
        </div>
      ) : null}

      {execution ? (
        <div className="mt-4 rounded-xl border border-emerald-300/25 bg-emerald-300/[0.055] px-3 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs font-semibold text-emerald-100">
              ORBITINTEL RESULT · 200 OK
            </p>
            <span className="rounded-full border border-emerald-300/20 px-2 py-1 text-[9px] font-semibold uppercase tracking-[0.1em] text-emerald-200">
              {execution.replayed_result ? "stored replay" : "executed once"}
            </span>
          </div>
          <dl className="mt-3 grid gap-2 text-[10px] sm:grid-cols-2">
            <div>
              <dt className="text-slate-600">Execution</dt>
              <dd className="mt-1 break-all font-mono text-slate-400">{execution.execution_id}</dd>
            </div>
            <div>
              <dt className="text-slate-600">Completed</dt>
              <dd className="mt-1 text-slate-400">{formatTimestamp(execution.completed_at)}</dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-slate-600">Result hash · {execution.result_size_bytes} bytes</dt>
              <dd className="mt-1 break-all font-mono text-slate-400">{execution.result_hash}</dd>
            </div>
          </dl>
          <pre className="mt-3 max-h-[32rem] overflow-auto rounded-lg bg-black/35 p-3 text-[10px] leading-4 text-emerald-50/85">
            {prettyJson(execution.result)}
          </pre>
          <div className="mt-3 rounded-lg border border-white/[0.07] bg-black/20 px-3 py-2.5 text-[10px] leading-4 text-slate-400">
            <strong className="text-slate-200">Data source: CelesTrak GP data.</strong>{" "}
            OrbitIntel outputs are deterministic informational heuristics from a current orbital snapshot. They are not conjunction screening, a collision warning, or an operational tracking service.
          </div>
        </div>
      ) : null}
    </section>
  );
}
