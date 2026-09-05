"use client";

import { PurchaseReceipt } from "@/components/purchase-receipt";

import Script from "next/script";
import { useCallback, useEffect, useRef, useState } from "react";

import { useAccountSession } from "@/components/account-session";
import {
  PaidResourceAccess,
  type CommerceOutcome,
  type CompensationSummary,
  type RefundSummary,
} from "@/components/paid-resource-access";
import { ApiRequestFailure } from "@/lib/api-client";
import {
  createRazorpayCheckout,
  isRazorpayCheckoutAvailable,
  RAZORPAY_CHECKOUT_SCRIPT_ID,
  RAZORPAY_CHECKOUT_SCRIPT_SRC,
  type RazorpayCheckoutInstance,
  type RazorpayCheckoutSuccess,
} from "@/lib/razorpay-checkout";

type PaymentTransactionState =
  | "order_creation_pending"
  | "order_created"
  | "order_creation_failed"
  | "order_creation_uncertain"
  | "payment_pending"
  | "payment_authorized"
  | "paid"
  | "reconciliation_required";

type CheckoutConfiguration = {
  key_id: string;
  order_id: string;
  amount: number;
  currency: string;
  name: string;
  description: string;
};

type PaymentAttempt = {
  id: string;
  provider_payment_id: string;
  status: "created" | "authorized" | "captured" | "failed" | "refunded";
  captured: boolean;
};

type PaymentTransaction = {
  transaction_id: string;
  state: PaymentTransactionState;
  provider: "razorpay";
  authorization_id: string;
  merchant_id: string;
  service_id: string;
  amount: number;
  currency: string;
  purchase_type: "one_time";
  provider_order_id: string | null;
  provider_order_status: "created" | "attempted" | "paid" | null;
  attempts: PaymentAttempt[];
  checkout: CheckoutConfiguration | null;
  compensation_summary: CompensationSummary | null;
  refund_summary: RefundSummary | null;
  commerce_outcome: CommerceOutcome;
};

export type CheckoutAuthorization = {
  id: string;
  state: "active" | "expired";
  merchantId: string;
  merchantName: string;
  serviceId: string;
  serviceName: string;
  amount: number;
  currency: string;
  purchase_type: string;
  expires_at: string;
};

type DisplayFailure = {
  code: string;
  message: string;
};

type CheckoutOperation =
  | "idle"
  | "creating"
  | "checkout_open"
  | "monitoring"
  | "refreshing"
  | "reconciling"
  | "verifying";

class CheckoutRequestFailure extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "CheckoutRequestFailure";
  }
}

const transactionStates = new Set<PaymentTransactionState>([
  "order_creation_pending",
  "order_created",
  "order_creation_failed",
  "order_creation_uncertain",
  "payment_pending",
  "payment_authorized",
  "paid",
  "reconciliation_required",
]);
const checkoutStates = new Set<PaymentTransactionState>([
  "order_created",
  "payment_pending",
]);
const pollableStates = new Set<PaymentTransactionState>([
  "order_creation_pending",
  "order_created",
  "order_creation_uncertain",
  "payment_pending",
  "payment_authorized",
  "reconciliation_required",
]);
const transactionIdPattern = /^txn_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const merchantIdPattern = /^mrc_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const serviceIdPattern = /^svc_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const attemptIdPattern = /^pmt_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const orderIdPattern = /^order_[A-Za-z0-9]{1,58}$/;
const paymentIdPattern = /^pay_[A-Za-z0-9]{1,60}$/;
const compensationIdPattern = /^cmp_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const refundIdPattern = /^rfd_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
const providerRefundIdPattern = /^rfnd_[A-Za-z0-9]{1,59}$/;
const signaturePattern = /^[0-9a-fA-F]{64}$/;
const keyIdPattern = /^rzp_test_[A-Za-z0-9]{8,64}$/;
const attemptStatuses = new Set<PaymentAttempt["status"]>([
  "created",
  "authorized",
  "captured",
  "failed",
  "refunded",
]);
const commerceOutcomes = new Set<CommerceOutcome>([
  "payment_pending",
  "paid",
  "fulfillment_pending",
  "fulfilled",
  "compensation_pending",
  "manual_review",
  "refunded",
]);
const compensationDecisionStates = new Set<CompensationSummary["decision_state"]>([
  "pending",
  "approved",
  "rejected",
  "executing",
  "completed",
  "manual_review",
]);
const refundStates = new Set<RefundSummary["state"]>([
  "refund_pending",
  "refund_processing",
  "refunded",
  "refund_failed",
  "refund_uncertain",
  "reconciliation_required",
]);
const recoveryPollableOutcomes = new Set<CommerceOutcome>([
  "paid",
  "fulfillment_pending",
  "compensation_pending",
  "manual_review",
]);
const integerFormatter = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 0,
});

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

function isTransactionState(value: unknown): value is PaymentTransactionState {
  return (
    typeof value === "string" &&
    transactionStates.has(value as PaymentTransactionState)
  );
}

function parseCheckoutConfiguration(
  value: unknown,
  transactionId: string,
  amount: number,
  currency: string,
): CheckoutConfiguration | null {
  if (!isRecord(value)) {
    return null;
  }

  const name = safeText(value.name, 200);
  const description = safeText(value.description, 255);
  if (
    value.transaction_id !== transactionId ||
    value.provider !== "razorpay" ||
    !isNonEmptyString(value.key_id) ||
    !keyIdPattern.test(value.key_id) ||
    !isNonEmptyString(value.order_id) ||
    !orderIdPattern.test(value.order_id) ||
    value.amount !== amount ||
    value.currency !== currency ||
    !name ||
    !description
  ) {
    return null;
  }

  return {
    key_id: value.key_id,
    order_id: value.order_id,
    amount,
    currency,
    name,
    description,
  };
}

function parsePaymentAttempts(
  value: unknown,
  amount: number,
  currency: string,
): PaymentAttempt[] | null {
  if (!Array.isArray(value) || value.length > 100) {
    return null;
  }

  const attempts: PaymentAttempt[] = [];
  const providerPaymentIds = new Set<string>();
  for (const candidate of value) {
    if (
      !isRecord(candidate) ||
      !isNonEmptyString(candidate.id) ||
      !attemptIdPattern.test(candidate.id) ||
      !isNonEmptyString(candidate.provider_payment_id) ||
      !paymentIdPattern.test(candidate.provider_payment_id) ||
      providerPaymentIds.has(candidate.provider_payment_id) ||
      !isNonEmptyString(candidate.status) ||
      !attemptStatuses.has(candidate.status as PaymentAttempt["status"]) ||
      candidate.amount !== amount ||
      candidate.currency !== currency ||
      typeof candidate.captured !== "boolean" ||
      (candidate.captured &&
        candidate.status !== "captured" &&
        candidate.status !== "refunded") ||
      (candidate.status === "captured" && !candidate.captured)
    ) {
      return null;
    }
    providerPaymentIds.add(candidate.provider_payment_id);
    attempts.push({
      id: candidate.id,
      provider_payment_id: candidate.provider_payment_id,
      status: candidate.status as PaymentAttempt["status"],
      captured: candidate.captured,
    });
  }
  return attempts;
}

function parseCompensationSummary(value: unknown): CompensationSummary | null | undefined {
  if (value === null) {
    return null;
  }
  if (!isRecord(value)) {
    return undefined;
  }
  const recommendedAction = safeText(value.recommended_action, 100);
  const decisionReasonCode = safeText(value.decision_reason_code, 100);
  const failureCode = safeText(value.failure_code, 100);
  const approvedRefundAmount =
    value.approved_refund_amount === null
      ? null
      : typeof value.approved_refund_amount === "number" &&
          Number.isSafeInteger(value.approved_refund_amount) &&
          value.approved_refund_amount > 0
        ? value.approved_refund_amount
        : undefined;
  if (
    !isNonEmptyString(value.compensation_id) ||
    !compensationIdPattern.test(value.compensation_id) ||
    !isNonEmptyString(value.decision_state) ||
    !compensationDecisionStates.has(
      value.decision_state as CompensationSummary["decision_state"],
    ) ||
    !recommendedAction ||
    approvedRefundAmount === undefined ||
    !decisionReasonCode ||
    !failureCode ||
    !isNonEmptyString(value.fulfillment_execution_id) ||
    !/^ful_[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(
      value.fulfillment_execution_id,
    )
  ) {
    return undefined;
  }
  return {
    compensation_id: value.compensation_id,
    decision_state: value.decision_state as CompensationSummary["decision_state"],
    recommended_action: recommendedAction,
    approved_refund_amount: approvedRefundAmount,
    decision_reason_code: decisionReasonCode,
    failure_code: failureCode,
    fulfillment_execution_id: value.fulfillment_execution_id,
  };
}

function parseRefundSummary(value: unknown): RefundSummary | null | undefined {
  if (value === null) {
    return null;
  }
  if (!isRecord(value)) {
    return undefined;
  }
  const providerRefundId =
    value.provider_refund_id === null
      ? null
      : isNonEmptyString(value.provider_refund_id) &&
          providerRefundIdPattern.test(value.provider_refund_id)
        ? value.provider_refund_id
        : undefined;
  const providerStatus =
    value.provider_status === null
      ? null
      : safeText(value.provider_status, 100) ?? undefined;
  const reconciliationReasonCode =
    value.reconciliation_reason_code === null ||
    value.reconciliation_reason_code === undefined
      ? null
      : safeText(value.reconciliation_reason_code, 100) ?? undefined;
  if (
    !isNonEmptyString(value.refund_id) ||
    !refundIdPattern.test(value.refund_id) ||
    !isNonEmptyString(value.provider_payment_id) ||
    !paymentIdPattern.test(value.provider_payment_id) ||
    providerRefundId === undefined ||
    typeof value.amount !== "number" ||
    !Number.isSafeInteger(value.amount) ||
    value.amount < 1 ||
    !isNonEmptyString(value.currency) ||
    !/^[A-Z]{3}$/.test(value.currency) ||
    !isNonEmptyString(value.state) ||
    !refundStates.has(value.state as RefundSummary["state"]) ||
    providerStatus === undefined ||
    reconciliationReasonCode === undefined
  ) {
    return undefined;
  }
  return {
    refund_id: value.refund_id,
    provider_payment_id: value.provider_payment_id,
    provider_refund_id: providerRefundId,
    amount: value.amount,
    currency: value.currency,
    state: value.state as RefundSummary["state"],
    provider_status: providerStatus,
    reconciliation_reason_code: reconciliationReasonCode,
  };
}

function parsePaymentTransaction(
  value: unknown,
  authorization: CheckoutAuthorization,
  expectedTransactionId?: string,
): PaymentTransaction | null {
  if (!isRecord(value)) {
    return null;
  }

  const merchant = value.merchant;
  const service = value.service;
  const attempts = parsePaymentAttempts(value.attempts, value.amount as number, value.currency as string);
  const providerOrderId =
    value.provider_order_id === null
      ? null
      : isNonEmptyString(value.provider_order_id) &&
          orderIdPattern.test(value.provider_order_id)
        ? value.provider_order_id
        : undefined;
  const providerOrderStatus =
    value.provider_order_status === null ||
    value.provider_order_status === "created" ||
    value.provider_order_status === "attempted" ||
    value.provider_order_status === "paid"
      ? value.provider_order_status
      : undefined;
  const compensationSummary = parseCompensationSummary(value.compensation_summary);
  const refundSummary = parseRefundSummary(value.refund_summary);
  if (
    !isNonEmptyString(value.transaction_id) ||
    !transactionIdPattern.test(value.transaction_id) ||
    (expectedTransactionId !== undefined &&
      value.transaction_id !== expectedTransactionId) ||
    !isTransactionState(value.state) ||
    value.provider !== "razorpay" ||
    value.authorization_id !== authorization.id ||
    !isRecord(merchant) ||
    !isNonEmptyString(merchant.id) ||
    !merchantIdPattern.test(merchant.id) ||
    merchant.id !== authorization.merchantId ||
    !safeText(merchant.name, 200) ||
    !isRecord(service) ||
    !isNonEmptyString(service.id) ||
    !serviceIdPattern.test(service.id) ||
    service.id !== authorization.serviceId ||
    !safeText(service.name, 200) ||
    typeof value.amount !== "number" ||
    !Number.isSafeInteger(value.amount) ||
    value.amount < 1 ||
    value.amount !== authorization.amount ||
    !isNonEmptyString(value.currency) ||
    !/^[A-Z]{3}$/.test(value.currency) ||
    value.currency !== authorization.currency ||
    value.purchase_type !== "one_time" ||
    value.purchase_type !== authorization.purchase_type ||
    providerOrderId === undefined ||
    providerOrderStatus === undefined ||
    ((providerOrderId === null) !== (providerOrderStatus === null)) ||
    !attempts ||
    !("checkout" in value) ||
    compensationSummary === undefined ||
    refundSummary === undefined ||
    !isNonEmptyString(value.commerce_outcome) ||
    !commerceOutcomes.has(value.commerce_outcome as CommerceOutcome)
  ) {
    return null;
  }

  const requiresCheckout = checkoutStates.has(value.state);
  const checkout =
    value.checkout === null
      ? null
      : parseCheckoutConfiguration(
          value.checkout,
          value.transaction_id,
          value.amount,
          value.currency,
        );
  if (
    (requiresCheckout && !checkout) ||
    (!requiresCheckout && value.checkout !== null)
  ) {
    return null;
  }
  if (checkout && checkout.order_id !== providerOrderId) {
    return null;
  }

  return {
    transaction_id: value.transaction_id,
    state: value.state,
    provider: "razorpay",
    authorization_id: value.authorization_id,
    merchant_id: merchant.id,
    service_id: service.id,
    amount: value.amount,
    currency: value.currency,
    purchase_type: "one_time",
    provider_order_id: providerOrderId,
    provider_order_status: providerOrderStatus,
    attempts,
    checkout,
    compensation_summary: compensationSummary,
    refund_summary: refundSummary,
    commerce_outcome: value.commerce_outcome as CommerceOutcome,
  };
}

function parseCheckoutSuccess(
  value: unknown,
  expectedOrderId: string,
): RazorpayCheckoutSuccess | null {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.razorpay_order_id) ||
    value.razorpay_order_id !== expectedOrderId ||
    !isNonEmptyString(value.razorpay_payment_id) ||
    !paymentIdPattern.test(value.razorpay_payment_id) ||
    !isNonEmptyString(value.razorpay_signature) ||
    !signaturePattern.test(value.razorpay_signature)
  ) {
    return null;
  }

  return {
    razorpay_order_id: value.razorpay_order_id,
    razorpay_payment_id: value.razorpay_payment_id,
    razorpay_signature: value.razorpay_signature,
  };
}

function failureFrom(error: unknown): DisplayFailure {
  if (error instanceof ApiRequestFailure) {
    return { code: error.code, message: error.message };
  }
  if (error instanceof CheckoutRequestFailure) {
    return { code: error.code, message: error.message };
  }

  return {
    code: "PAYMENT_REQUEST_FAILED",
    message: "The payment operation could not be completed.",
  };
}

function formatAmount(amount: number, currency: string): string {
  if (currency === "INR") {
    const rupees = Math.trunc(amount / 100);
    const paise = amount % 100;
    return `₹${integerFormatter.format(rupees)}.${paise.toString().padStart(2, "0")}`;
  }

  return `${currency} ${integerFormatter.format(amount)} minor units`;
}

function formatState(state: PaymentTransactionState): string {
  return state
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function stateExplanation(state: PaymentTransactionState): string {
  switch (state) {
    case "order_creation_pending":
      return "MeterGate is durably preparing the server-side Razorpay order.";
    case "order_created":
      return "The server-created Razorpay order is ready for Test Mode Checkout.";
    case "order_creation_failed":
      return "The server could not create the Razorpay order. No successful payment is recorded.";
    case "order_creation_uncertain":
      return "The provider outcome is uncertain. Reconcile before attempting another order.";
    case "payment_pending":
      return "The order exists, but MeterGate has not verified a captured payment.";
    case "payment_authorized":
      return "Razorpay authorized the payment; capture is still awaiting backend confirmation.";
    case "paid":
      return "MeterGate verified the captured payment from trusted backend evidence.";
    case "reconciliation_required":
      return "MeterGate needs a server-to-server Razorpay status check.";
  }
}

function commerceOutcomeExplanation(outcome: CommerceOutcome): string {
  switch (outcome) {
    case "payment_pending":
      return "MeterGate is still establishing authoritative payment state.";
    case "paid":
      return "Payment is verified and the immutable entitlement is being prepared.";
    case "fulfillment_pending":
      return "Payment remains paid while OrbitIntel fulfillment is in progress.";
    case "fulfilled":
      return "The paid OrbitIntel result was fulfilled successfully.";
    case "compensation_pending":
      return "Fulfillment failed permanently. Access is quarantined while compensation is processed.";
    case "manual_review":
      return "Access remains quarantined while MeterGate reconciles uncertain refund evidence.";
    case "refunded":
      return "MeterGate confirms the Test Mode refund completed. The original payment history remains paid.";
  }
}

function formatCode(value: string): string {
  return value.replaceAll("_", " ");
}

function RecoveryStatusPanel({
  transaction,
  busy,
  onRefresh,
  onReconcile,
}: {
  transaction: PaymentTransaction;
  busy: boolean;
  onRefresh: () => void;
  onReconcile: () => void;
}) {
  const compensation = transaction.compensation_summary;
  const refund = transaction.refund_summary;
  if (!compensation && !refund) {
    return null;
  }

  const refundConfirmed =
    transaction.commerce_outcome === "refunded" && refund?.state === "refunded";
  const refundStatusUncertain =
    refund?.state === "refund_uncertain" ||
    refund?.state === "reconciliation_required";
  const needsManualReview =
    transaction.commerce_outcome === "manual_review" ||
    compensation?.decision_state === "manual_review" ||
    refundStatusUncertain ||
    refund?.state === "refund_failed" ||
    (refund?.state === "refunded" &&
      transaction.commerce_outcome !== "refunded");
  const refundProcessing =
    !refundConfirmed &&
    !needsManualReview &&
    (refund?.state === "refund_pending" ||
      refund?.state === "refund_processing" ||
      compensation?.decision_state === "executing");
  const statusLabel = refundConfirmed
    ? "REFUND COMPLETED"
    : needsManualReview
      ? refund?.state === "refund_failed"
        ? "REFUND FAILED · MANUAL REVIEW"
        : refundStatusUncertain
          ? "REFUND STATUS UNCERTAIN"
          : "COMPENSATION · MANUAL REVIEW"
      : refundProcessing
        ? "REFUND PROCESSING"
        : "COMPENSATION REQUIRED";
  const statusDescription = refundConfirmed
    ? "The backend and local refund state both confirm completion. This did not rewrite the original paid transaction or entitlement evidence."
    : needsManualReview
      ? refund
        ? "MeterGate cannot safely claim refund completion from the available provider evidence. Access stays quarantined while reconciliation continues."
        : "The compensation decision needs operator review. No refund completion is claimed, and paid access remains quarantined."
      : refundProcessing
        ? "The refund request is durable and processing asynchronously. MeterGate will reconcile provider evidence before declaring completion."
        : "OrbitIntel fulfillment failed permanently with no result. MeterGate has quarantined access and is applying the deterministic compensation policy.";
  const tone = refundConfirmed
    ? "border-emerald-300/25 bg-emerald-300/[0.055]"
    : needsManualReview
      ? "border-amber-300/25 bg-amber-300/[0.055]"
      : "border-rose-300/20 bg-rose-300/[0.05]";
  const titleTone = refundConfirmed
    ? "text-emerald-100"
    : needsManualReview
      ? "text-amber-100"
      : "text-rose-100";

  return (
    <div className={`mt-4 rounded-xl border px-3 py-3 ${tone}`} aria-live="polite">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-rose-200/75">
            FULFILLMENT FAILED
          </p>
          <p className={`mt-1 text-sm font-semibold ${titleTone}`}>
            {statusLabel}
          </p>
        </div>
        <span className="rounded-full border border-amber-300/25 bg-black/15 px-2.5 py-1 text-[9px] font-semibold uppercase tracking-[0.12em] text-amber-100">
          Test Mode · no real money
        </span>
      </div>
      <p className="mt-2 text-xs font-medium leading-5 text-slate-100">
        Payment was captured, but the service could not be delivered.
      </p>
      <p className="mt-2 text-xs leading-5 text-slate-200/85">
        {statusDescription}
      </p>
      <p className="mt-2 text-[10px] leading-4 text-amber-50/75">
        TEST MODE — NO REAL MONEY MOVED. Any refund shown here is Razorpay Test
        Mode evidence verified by the MeterGate backend.
      </p>

      <dl className="mt-3 grid gap-3 border-t border-white/[0.08] pt-3 text-[10px] sm:grid-cols-2">
        <div className="sm:col-span-2">
          <dt className="text-slate-600">Payment transaction</dt>
          <dd className="mt-1 break-all font-mono text-slate-300">
            {transaction.transaction_id}
          </dd>
        </div>
        {compensation ? (
          <>
            <div>
              <dt className="text-slate-600">Compensation case</dt>
              <dd className="mt-1 break-all font-mono text-slate-300">
                {compensation.compensation_id}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Fulfillment execution</dt>
              <dd className="mt-1 break-all font-mono text-slate-300">
                {compensation.fulfillment_execution_id}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Decision</dt>
              <dd className="mt-1 text-slate-300">
                {formatCode(compensation.decision_state)} · {formatCode(compensation.recommended_action)}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Approved refund</dt>
              <dd className="mt-1 text-slate-300">
                {compensation.approved_refund_amount === null
                  ? "Pending authoritative decision"
                  : formatAmount(
                      compensation.approved_refund_amount,
                      transaction.currency,
                    )}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Failure reason</dt>
              <dd className="mt-1 break-all font-mono text-rose-100/75">
                {compensation.failure_code}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Decision reason</dt>
              <dd className="mt-1 break-all font-mono text-slate-300">
                {compensation.decision_reason_code}
              </dd>
            </div>
          </>
        ) : null}
        {refund ? (
          <>
            <div>
              <dt className="text-slate-600">Refund</dt>
              <dd className="mt-1 break-all font-mono text-slate-300">
                {refund.refund_id}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Captured Razorpay payment</dt>
              <dd className="mt-1 break-all font-mono text-slate-300">
                {refund.provider_payment_id}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Razorpay refund</dt>
              <dd className="mt-1 break-all font-mono text-slate-300">
                {refund.provider_refund_id ?? "Awaiting provider identifier"}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Refund amount</dt>
              <dd className="mt-1 text-slate-300">
                {formatAmount(refund.amount, refund.currency)} · {refund.currency}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Local refund state</dt>
              <dd className="mt-1 font-mono text-slate-300">
                {refund.state === "refunded" && !refundConfirmed
                  ? "confirmation_pending"
                  : refund.state}
              </dd>
            </div>
            <div>
              <dt className="text-slate-600">Provider status</dt>
              <dd className="mt-1 font-mono text-slate-300">
                {refund.provider_status ?? "Not yet confirmed"}
              </dd>
            </div>
            {refund.reconciliation_reason_code ? (
              <div>
                <dt className="text-slate-600">Reconciliation reason</dt>
                <dd className="mt-1 break-all font-mono text-amber-100/80">
                  {refund.reconciliation_reason_code}
                </dd>
              </div>
            ) : null}
          </>
        ) : null}
      </dl>
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={onRefresh}
          className="rounded-lg border border-white/15 bg-black/10 px-3 py-2 text-xs font-medium text-slate-100 transition hover:border-white/25 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-wait disabled:opacity-50"
        >
          {busy ? "Refreshing recovery status…" : "Refresh recovery status"}
        </button>
        {refundStatusUncertain ? (
          <button
            type="button"
            disabled={busy}
            onClick={onReconcile}
            className="rounded-lg border border-violet-300/20 bg-violet-300/[0.055] px-3 py-2 text-xs font-medium text-violet-100 transition hover:border-violet-300/35 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-200 disabled:cursor-wait disabled:opacity-50"
          >
            {busy ? "Reconciling refund…" : "Reconcile refund safely"}
          </button>
        ) : null}
      </div>
    </div>
  );
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

export function StandardCheckout({
  apiBaseEndpoint,
  authorization,
}: {
  apiBaseEndpoint: string;
  authorization: CheckoutAuthorization;
}) {
  const { requestAuthenticated } = useAccountSession();
  const [scriptState, setScriptState] = useState<
    "loading" | "ready" | "failed"
  >("loading");
  const [operation, setOperation] = useState<CheckoutOperation>("idle");
  const [transaction, setTransaction] = useState<PaymentTransaction | null>(null);
  const [failure, setFailure] = useState<DisplayFailure | null>(null);
  const [pollFailure, setPollFailure] = useState<DisplayFailure | null>(null);
  const [activityMessage, setActivityMessage] = useState<string | null>(null);
  const [pollCycle, setPollCycle] = useState(0);
  const [authorizationExpired, setAuthorizationExpired] = useState(
    () =>
      authorization.state === "expired" ||
      Date.now() >= Date.parse(authorization.expires_at),
  );
  const mountedRef = useRef(false);
  const requestSequenceRef = useRef(0);
  const operationControllerRef = useRef<AbortController | null>(null);
  const pollControllerRef = useRef<AbortController | null>(null);
  const pollFailureCountRef = useRef(0);
  const checkoutRef = useRef<RazorpayCheckoutInstance | null>(null);

  useEffect(() => {
    mountedRef.current = true;

    return () => {
      mountedRef.current = false;
      requestSequenceRef.current += 1;
      operationControllerRef.current?.abort();
      pollControllerRef.current?.abort();
      checkoutRef.current?.close();
      checkoutRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (authorizationExpired) {
      return;
    }

    const remaining = Date.parse(authorization.expires_at) - Date.now();
    const timeout = window.setTimeout(
      () => setAuthorizationExpired(true),
      Math.max(0, remaining),
    );
    return () => window.clearTimeout(timeout);
  }, [authorization.expires_at, authorizationExpired]);

  const acceptTransaction = useCallback((next: PaymentTransaction) => {
    setTransaction((current) =>
      current?.state === "paid" && next.state !== "paid" ? current : next,
    );
    setPollFailure(null);
    pollFailureCountRef.current = 0;
    if (next.state === "paid") {
      setFailure(null);
      setActivityMessage(commerceOutcomeExplanation(next.commerce_outcome));
    }
  }, []);

  const beginServerOperation = useCallback(() => {
    pollControllerRef.current?.abort();
    operationControllerRef.current?.abort();
    const controller = new AbortController();
    operationControllerRef.current = controller;
    const sequence = requestSequenceRef.current + 1;
    requestSequenceRef.current = sequence;
    return { controller, sequence };
  }, []);

  const verifyCheckout = useCallback(
    async (
      transactionAtCheckout: PaymentTransaction,
      checkoutResponse: unknown,
    ) => {
      const checkout = transactionAtCheckout.checkout;
      if (!checkout) {
        return;
      }

      const evidence = parseCheckoutSuccess(
        checkoutResponse,
        checkout.order_id,
      );
      if (!evidence) {
        setOperation("monitoring");
        setFailure({
          code: "PAYMENT_CHECKOUT_RESPONSE_INVALID",
          message:
            "Razorpay returned unexpected checkout evidence. MeterGate will continue checking backend state.",
        });
        setPollCycle((current) => current + 1);
        return;
      }

      const { controller, sequence } = beginServerOperation();
      setOperation("verifying");
      setFailure(null);
      setActivityMessage(
        "Checkout returned payment evidence. MeterGate is verifying it on the server.",
      );
      try {
        const body = await requestAuthenticated(
          `${apiBaseEndpoint}/payment-transactions/${encodeURIComponent(transactionAtCheckout.transaction_id)}/checkout/verify`,
          {
            method: "POST",
            body: evidence,
            signal: controller.signal,
          },
        );
        const verified = parsePaymentTransaction(
          body,
          authorization,
          transactionAtCheckout.transaction_id,
        );
        if (!verified) {
          throw new CheckoutRequestFailure(
            "PAYMENT_RESPONSE_INVALID",
            "The server returned a payment result that did not match this authorization.",
          );
        }
        if (mountedRef.current && requestSequenceRef.current === sequence) {
          acceptTransaction(verified);
          setOperation("monitoring");
          if (verified.state !== "paid") {
            setActivityMessage(
              "The callback was checked, but payment is not yet captured and verified. Backend monitoring continues.",
            );
          }
        }
      } catch (error: unknown) {
        if (
          mountedRef.current &&
          !controller.signal.aborted &&
          requestSequenceRef.current === sequence
        ) {
          setFailure(failureFrom(error));
          setOperation("monitoring");
          setActivityMessage(
            "The callback result is inconclusive. MeterGate will continue reading durable backend state.",
          );
          setPollCycle((current) => current + 1);
        }
      } finally {
        if (operationControllerRef.current === controller) {
          operationControllerRef.current = null;
        }
      }
    },
    [
      acceptTransaction,
      apiBaseEndpoint,
      authorization,
      beginServerOperation,
      requestAuthenticated,
    ],
  );

  const openCheckout = useCallback(
    (transactionAtCheckout: PaymentTransaction) => {
      const checkout = transactionAtCheckout.checkout;
      if (!checkout) {
        setFailure({
          code: "PAYMENT_CHECKOUT_NOT_READY",
          message: "The server-side Razorpay order is not ready for Checkout.",
        });
        return;
      }
      if (scriptState !== "ready" || !isRazorpayCheckoutAvailable()) {
        setFailure({
          code: "PAYMENT_CHECKOUT_SCRIPT_UNAVAILABLE",
          message:
            "Razorpay Standard Checkout did not load. The existing transaction remains safe to retry after reloading the page.",
        });
        return;
      }
      if (!checkout.key_id.startsWith("rzp_test_")) {
        setFailure({
          code: "PAYMENT_TEST_MODE_REQUIRED",
          message:
            "MeterGate refused to open Checkout because the server did not return a Razorpay Test Mode key.",
        });
        return;
      }
      if (checkoutRef.current) {
        return;
      }

      let completionStarted = false;
      try {
        const checkoutInstance = createRazorpayCheckout({
          key: checkout.key_id,
          order_id: checkout.order_id,
          amount: checkout.amount,
          currency: checkout.currency,
          name: checkout.name,
          description: checkout.description,
          handler: (response) => {
            completionStarted = true;
            checkoutRef.current = null;
            void verifyCheckout(transactionAtCheckout, response);
          },
          modal: {
            confirm_close: true,
            ondismiss: () => {
              checkoutRef.current = null;
              if (!mountedRef.current || completionStarted) {
                return;
              }
              setOperation("monitoring");
              setActivityMessage(
                "Checkout closed. This does not prove cancellation; MeterGate is checking backend state.",
              );
              setPollCycle((current) => current + 1);
            },
          },
          retry: { enabled: true },
        });
        checkoutInstance.on("payment.failed", () => {
          if (!mountedRef.current) {
            return;
          }
          setFailure({
            code: "PAYMENT_ATTEMPT_FAILED",
            message:
              "Razorpay reported that this attempt failed. The transaction is not marked paid; you may retry inside Checkout.",
          });
        });
        checkoutRef.current = checkoutInstance;
        setFailure(null);
        setOperation("checkout_open");
        setActivityMessage(
          "Razorpay Test Mode Checkout is open. Only backend verification can mark this transaction paid.",
        );
        checkoutInstance.open();
      } catch {
        checkoutRef.current = null;
        setOperation("monitoring");
        setFailure({
          code: "PAYMENT_CHECKOUT_OPEN_FAILED",
          message:
            "Razorpay Standard Checkout could not be opened. The existing transaction was not marked paid.",
        });
      }
    },
    [scriptState, verifyCheckout],
  );

  const createPaymentTransaction = async () => {
    if (transaction || operation !== "idle") {
      return;
    }
    if (
      authorizationExpired ||
      authorization.state !== "active" ||
      Date.now() >= Date.parse(authorization.expires_at)
    ) {
      setAuthorizationExpired(true);
      setFailure({
        code: "PAYMENT_AUTHORIZATION_EXPIRED",
        message:
          "This purchase authorization expired before payment creation. Complete a new trusted approval.",
      });
      return;
    }
    if (authorization.purchase_type !== "one_time") {
      setFailure({
        code: "PAYMENT_PURCHASE_TYPE_UNSUPPORTED",
        message:
          "This Standard Checkout milestone supports one-time purchases only.",
      });
      return;
    }
    if (authorization.amount <= 0) {
      setFailure({
        code: "PAYMENT_AMOUNT_UNSUPPORTED",
        message: "Razorpay Checkout requires a positive payable amount.",
      });
      return;
    }
    if (scriptState !== "ready") {
      setFailure({
        code: "PAYMENT_CHECKOUT_SCRIPT_UNAVAILABLE",
        message:
          "Wait for Razorpay Standard Checkout to load before creating the payment transaction.",
      });
      return;
    }

    const { controller, sequence } = beginServerOperation();
    setOperation("creating");
    setFailure(null);
    setPollFailure(null);
    setActivityMessage(
      "MeterGate is consuming this authorization into one idempotent server transaction.",
    );
    try {
      const body = await requestAuthenticated(
        `${apiBaseEndpoint}/payment-transactions`,
        {
          method: "POST",
          body: { authorization_id: authorization.id },
          signal: controller.signal,
        },
      );
      const created = parsePaymentTransaction(body, authorization);
      if (!created) {
        throw new CheckoutRequestFailure(
          "PAYMENT_RESPONSE_INVALID",
          "The server returned a payment transaction that did not match this authorization.",
        );
      }
      if (mountedRef.current && requestSequenceRef.current === sequence) {
        acceptTransaction(created);
        setOperation("monitoring");
        if (created.checkout) {
          openCheckout(created);
        } else if (created.state !== "paid") {
          setActivityMessage(
            "The transaction exists, but Checkout is not ready. MeterGate is polling backend state.",
          );
        }
      }
    } catch (error: unknown) {
      if (
        mountedRef.current &&
        !controller.signal.aborted &&
        requestSequenceRef.current === sequence
      ) {
        setOperation("idle");
        setFailure(failureFrom(error));
        setActivityMessage(null);
      }
    } finally {
      if (operationControllerRef.current === controller) {
        operationControllerRef.current = null;
      }
    }
  };

  const requestCurrentTransaction = async (kind: "refresh" | "reconcile") => {
    if (!transaction || (transaction.state === "paid" && kind === "reconcile")) {
      return;
    }

    const { controller, sequence } = beginServerOperation();
    setOperation(kind === "refresh" ? "refreshing" : "reconciling");
    setFailure(null);
    setPollFailure(null);
    setActivityMessage(
      kind === "refresh"
        ? "Reading durable MeterGate transaction state."
        : "MeterGate is reconciling this transaction with Razorpay server-to-server.",
    );
    try {
      const endpoint = `${apiBaseEndpoint}/payment-transactions/${encodeURIComponent(transaction.transaction_id)}${kind === "reconcile" ? "/reconcile" : ""}`;
      const body = await requestAuthenticated(endpoint, {
        method: kind === "reconcile" ? "POST" : "GET",
        signal: controller.signal,
      });
      const refreshed = parsePaymentTransaction(
        body,
        authorization,
        transaction.transaction_id,
      );
      if (!refreshed) {
        throw new CheckoutRequestFailure(
          "PAYMENT_RESPONSE_INVALID",
          "The server returned payment state that did not match this transaction.",
        );
      }
      if (mountedRef.current && requestSequenceRef.current === sequence) {
        acceptTransaction(refreshed);
        setOperation("monitoring");
        setActivityMessage(
          refreshed.state === "paid"
            ? commerceOutcomeExplanation(refreshed.commerce_outcome)
            : stateExplanation(refreshed.state),
        );
      }
    } catch (error: unknown) {
      if (
        mountedRef.current &&
        !controller.signal.aborted &&
        requestSequenceRef.current === sequence
      ) {
        setOperation("monitoring");
        setFailure(failureFrom(error));
      }
    } finally {
      if (operationControllerRef.current === controller) {
        operationControllerRef.current = null;
      }
    }
  };

  const requestRefundReconciliation = async () => {
    const refund = transaction?.refund_summary;
    if (
      !transaction ||
      !refund ||
      (refund.state !== "refund_uncertain" &&
        refund.state !== "reconciliation_required")
    ) {
      return;
    }

    const { controller, sequence } = beginServerOperation();
    setOperation("reconciling");
    setFailure(null);
    setPollFailure(null);
    setActivityMessage(
      "MeterGate is reconciling the reserved refund with Razorpay server-to-server.",
    );
    try {
      const refundBody = await requestAuthenticated(
        `${apiBaseEndpoint}/refunds/${encodeURIComponent(refund.refund_id)}/reconcile`,
        { method: "POST", signal: controller.signal },
      );
      const reconciledRefund = parseRefundSummary(refundBody);
      if (!reconciledRefund || reconciledRefund.refund_id !== refund.refund_id) {
        throw new CheckoutRequestFailure(
          "REFUND_RESPONSE_INVALID",
          "The server returned refund evidence that did not match this transaction.",
        );
      }
      const transactionBody = await requestAuthenticated(
        `${apiBaseEndpoint}/payment-transactions/${encodeURIComponent(transaction.transaction_id)}`,
        { method: "GET", signal: controller.signal },
      );
      const refreshed = parsePaymentTransaction(
        transactionBody,
        authorization,
        transaction.transaction_id,
      );
      if (!refreshed) {
        throw new CheckoutRequestFailure(
          "PAYMENT_RESPONSE_INVALID",
          "The server returned payment state that did not match this transaction.",
        );
      }
      if (mountedRef.current && requestSequenceRef.current === sequence) {
        acceptTransaction(refreshed);
        setOperation("monitoring");
        setActivityMessage(commerceOutcomeExplanation(refreshed.commerce_outcome));
      }
    } catch (error: unknown) {
      if (
        mountedRef.current &&
        !controller.signal.aborted &&
        requestSequenceRef.current === sequence
      ) {
        setOperation("monitoring");
        setFailure(failureFrom(error));
      }
    } finally {
      if (operationControllerRef.current === controller) {
        operationControllerRef.current = null;
      }
    }
  };

  useEffect(() => {
    if (
      !transaction ||
      operation !== "monitoring" ||
      (!pollableStates.has(transaction.state) &&
        !recoveryPollableOutcomes.has(transaction.commerce_outcome))
    ) {
      return;
    }

    const delay = Math.min(
      (transaction.commerce_outcome === "manual_review" ? 5_000 : 2_000) *
        2 ** Math.min(pollFailureCountRef.current, 2),
      8_000,
    );
    let controller: AbortController | null = null;
    const timeout = window.setTimeout(async () => {
      controller = new AbortController();
      pollControllerRef.current = controller;
      const sequence = requestSequenceRef.current + 1;
      requestSequenceRef.current = sequence;
      try {
        const body = await requestAuthenticated(
          `${apiBaseEndpoint}/payment-transactions/${encodeURIComponent(transaction.transaction_id)}`,
          { method: "GET", signal: controller.signal },
        );
        const refreshed = parsePaymentTransaction(
          body,
          authorization,
          transaction.transaction_id,
        );
        if (!refreshed) {
          throw new CheckoutRequestFailure(
            "PAYMENT_RESPONSE_INVALID",
            "The server returned payment state that did not match this transaction.",
          );
        }
        if (mountedRef.current && requestSequenceRef.current === sequence) {
          acceptTransaction(refreshed);
        }
      } catch (error: unknown) {
        if (
          mountedRef.current &&
          !controller.signal.aborted &&
          requestSequenceRef.current === sequence
        ) {
          pollFailureCountRef.current += 1;
          setPollFailure(failureFrom(error));
          setPollCycle((current) => current + 1);
        }
      } finally {
        if (pollControllerRef.current === controller) {
          pollControllerRef.current = null;
        }
      }
    }, delay);

    return () => {
      window.clearTimeout(timeout);
      controller?.abort();
      if (pollControllerRef.current === controller) {
        pollControllerRef.current = null;
      }
    };
  }, [
    acceptTransaction,
    apiBaseEndpoint,
    authorization,
    operation,
    pollCycle,
    requestAuthenticated,
    transaction,
  ]);

  const operationBusy =
    operation === "creating" ||
    operation === "refreshing" ||
    operation === "reconciling" ||
    operation === "verifying";
  const paymentVerified = transaction?.state === "paid";
  const canCreate =
    !transaction &&
    !authorizationExpired &&
    authorization.state === "active" &&
    authorization.purchase_type === "one_time" &&
    authorization.amount > 0 &&
    scriptState === "ready" &&
    !operationBusy;
  const checkoutReady =
    transaction?.checkout !== null && transaction?.checkout !== undefined;
  const latestFailedAttempt = transaction
    ? [...transaction.attempts]
        .reverse()
        .find((attempt) => attempt.status === "failed")
    : undefined;
  const capturedAttempt = transaction?.attempts.find(
    (attempt) => attempt.captured,
  );

  return (
    <section className="mt-4 rounded-2xl border border-cyan-300/20 bg-cyan-300/[0.035] p-4">
      <Script
        id={RAZORPAY_CHECKOUT_SCRIPT_ID}
        src={RAZORPAY_CHECKOUT_SCRIPT_SRC}
        strategy="afterInteractive"
        onReady={() => {
          if (isRazorpayCheckoutAvailable()) {
            setScriptState("ready");
          } else {
            setScriptState("failed");
            setFailure({
              code: "PAYMENT_CHECKOUT_SCRIPT_UNAVAILABLE",
              message:
                "Razorpay Standard Checkout loaded without its required browser API. No payment transaction was created by this failure.",
            });
          }
        }}
        onError={() => {
          setScriptState("failed");
          setFailure({
            code: "PAYMENT_CHECKOUT_SCRIPT_UNAVAILABLE",
            message:
              "Razorpay Standard Checkout could not be loaded. No payment transaction was created by this failure.",
          });
        }}
      />

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-cyan-200/75">
            PAYMENT READY · Razorpay Standard Checkout
          </p>
          <h6 className="mt-2 text-base font-semibold text-white">
            Server-gated Test Mode payment
          </h6>
        </div>
        <span className="rounded-full border border-amber-300/25 bg-amber-300/[0.08] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-amber-100">
          Test Mode
        </span>
      </div>

      <p className="mt-3 rounded-xl border border-amber-300/15 bg-amber-300/[0.045] px-3 py-2.5 text-xs leading-5 text-amber-50/90">
        <strong>TEST MODE — NO REAL MONEY WILL BE CHARGED.</strong> MeterGate
        creates the order on the server and treats only exact backend state{" "}
        <span className="font-mono">paid</span> as success.
      </p>

      <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2">
        <div>
          <dt className="text-slate-500">Merchant</dt>
          <dd className="mt-1 text-slate-200">{authorization.merchantName}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Service</dt>
          <dd className="mt-1 text-slate-200">{authorization.serviceName}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Final amount</dt>
          <dd className="mt-1 text-base font-semibold text-white">
            {formatAmount(authorization.amount, authorization.currency)} {authorization.currency}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Purchase type</dt>
          <dd className="mt-1 text-slate-200">One time</dd>
        </div>
      </dl>

      {transaction ? (
        <div
          className={`mt-4 rounded-xl border px-3 py-3 ${
            paymentVerified
              ? "border-emerald-300/20 bg-emerald-300/[0.055]"
              : "border-white/[0.08] bg-black/20"
          }`}
          aria-live="polite"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <p className="text-[10px] uppercase tracking-[0.15em] text-slate-500">
                Backend transaction state
              </p>
              <p
                className={`mt-1 text-sm font-semibold ${
                  paymentVerified ? "text-emerald-100" : "text-slate-100"
                }`}
              >
                {formatState(transaction.state)}
              </p>
            </div>
            <span className="font-mono text-[10px] text-slate-500">
              {transaction.provider}
            </span>
          </div>
          <p className="mt-2 text-xs leading-5 text-slate-300">
            {stateExplanation(transaction.state)}
          </p>
          {paymentVerified ? (
            <p className="mt-1 text-xs leading-5 text-slate-400">
              Commerce outcome: {commerceOutcomeExplanation(transaction.commerce_outcome)}
            </p>
          ) : null}
          <dl className="mt-3 space-y-2 border-t border-white/[0.07] pt-3 text-[10px]">
            <div>
              <dt className="text-slate-600">Transaction</dt>
              <dd className="mt-1 break-all font-mono text-slate-400">
                {transaction.transaction_id}
              </dd>
            </div>
            {transaction.provider_order_id ? (
              <div>
                <dt className="text-slate-600">Razorpay order</dt>
                <dd className="mt-1 break-all font-mono text-slate-400">
                  {transaction.provider_order_id}
                </dd>
              </div>
            ) : null}
            {capturedAttempt ? (
              <div>
                <dt className="text-slate-600">Captured Razorpay payment</dt>
                <dd className="mt-1 break-all font-mono text-slate-400">
                  {capturedAttempt.provider_payment_id}
                </dd>
              </div>
            ) : null}
            {latestFailedAttempt && !paymentVerified ? (
              <div>
                <dt className="text-rose-300/70">Payment attempt failed</dt>
                <dd className="mt-1 break-all font-mono text-rose-200/75">
                  {latestFailedAttempt.provider_payment_id}
                </dd>
                <dd className="mt-1 text-slate-400">
                  This attempt did not pay the order; another Test Mode attempt may still succeed.
                </dd>
              </div>
            ) : null}
          </dl>
        </div>
      ) : null}

      {transaction ? (
        <RecoveryStatusPanel
          transaction={transaction}
          busy={operation === "refreshing" || operation === "reconciling"}
          onRefresh={() => void requestCurrentTransaction("refresh")}
          onReconcile={() => void requestRefundReconciliation()}
        />
      ) : null}

      {!transaction ? (
        <button
          type="button"
          disabled={!canCreate}
          onClick={() => void createPaymentTransaction()}
          className="mt-4 w-full rounded-xl border border-cyan-300/25 bg-cyan-300/[0.09] px-4 py-3 text-sm font-semibold text-cyan-50 transition hover:border-cyan-300/40 hover:bg-cyan-300/[0.14] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {operation === "creating"
            ? "Creating server payment order…"
            : scriptState === "loading"
              ? "Loading Razorpay Test Checkout…"
              : authorizationExpired
                ? "Authorization expired"
                : `Pay ${formatAmount(authorization.amount, authorization.currency)} with Razorpay`}
        </button>
      ) : null}

      {transaction && !paymentVerified ? (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {checkoutReady ? (
            <button
              type="button"
              disabled={
                operationBusy ||
                operation === "checkout_open" ||
                scriptState !== "ready"
              }
              onClick={() => openCheckout(transaction)}
              className="rounded-xl border border-cyan-300/20 bg-cyan-300/[0.065] px-3 py-2.5 text-xs font-semibold text-cyan-100 transition hover:border-cyan-300/35 hover:bg-cyan-300/[0.1] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {operation === "checkout_open"
                ? "Razorpay Checkout open"
                : `Pay ${formatAmount(authorization.amount, authorization.currency)} with Razorpay`}
            </button>
          ) : null}
          <button
            type="button"
            disabled={operationBusy || operation === "checkout_open"}
            onClick={() => void requestCurrentTransaction("refresh")}
            className="rounded-xl border border-white/10 bg-white/[0.035] px-3 py-2.5 text-xs font-medium text-slate-300 transition hover:border-white/20 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-wait disabled:opacity-50"
          >
            {operation === "refreshing"
              ? "Refreshing backend state…"
              : "Refresh backend status"}
          </button>
          <button
            type="button"
            disabled={operationBusy || operation === "checkout_open"}
            onClick={() => void requestCurrentTransaction("reconcile")}
            className="rounded-xl border border-violet-300/15 bg-violet-300/[0.045] px-3 py-2.5 text-xs font-medium text-violet-100 transition hover:border-violet-300/30 hover:bg-violet-300/[0.08] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-200 disabled:cursor-wait disabled:opacity-50 sm:col-span-2"
          >
            {operation === "reconciling"
              ? "Reconciling with Razorpay…"
              : "Reconcile with Razorpay"}
          </button>
        </div>
      ) : null}

      {operation === "verifying" ? (
        <p role="status" className="mt-3 text-xs leading-5 text-slate-300">
          Verifying Checkout evidence on the MeterGate server. This browser event
          is not treated as payment success.
        </p>
      ) : null}
      {activityMessage ? (
        <p role="status" className="mt-3 text-xs leading-5 text-slate-400">
          {activityMessage}
        </p>
      ) : null}
      {failure ? <FailureNotice failure={failure} /> : null}
      {pollFailure ? (
        <FailureNotice
          failure={{
            code: "PAYMENT_STATUS_REFRESH_FAILED",
            message: `${pollFailure.message} The last displayed transaction state remains backend-derived.`,
          }}
        />
      ) : null}

      {paymentVerified && transaction ? (
        <PaidResourceAccess
          apiBaseEndpoint={apiBaseEndpoint}
          transactionId={transaction.transaction_id}
          commerceOutcome={transaction.commerce_outcome}
          compensationSummary={transaction.compensation_summary}
          refundSummary={transaction.refund_summary}
        />
      ) : null}
      {!paymentVerified && transaction ? <PurchaseReceipt apiBaseEndpoint={apiBaseEndpoint} transactionId={transaction.transaction_id} /> : null}
    </section>
  );
}
