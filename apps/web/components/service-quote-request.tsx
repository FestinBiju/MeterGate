"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";

type JsonPrimitive = boolean | null | number | string;
type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

type QuoteResponse = {
  id: string;
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
  input: JsonValue;
  input_hash: string;
  pricing: {
    amount: number;
    currency: string;
    purchase_type: string;
  };
  fulfillment: {
    maximum_seconds: number;
    refund_on_failure: boolean;
  };
  issued_at: string;
  expires_at: string;
  quote_hash: string;
  state: "active" | "expired";
};

type RequestState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "error"; message: string }
  | { kind: "resolved"; quote: QuoteResponse };

class QuoteRequestFailure extends Error {}

const REQUEST_TIMEOUT_MS = 8_000;
const MAX_EXPIRY_CHECK_INTERVAL_MS = 60_000;
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

function isQuoteResponse(value: unknown): value is QuoteResponse {
  if (!isRecord(value)) {
    return false;
  }

  const merchant = value.merchant;
  const service = value.service;
  const pricing = value.pricing;
  const fulfillment = value.fulfillment;

  return (
    isNonEmptyString(value.id) &&
    isRecord(merchant) &&
    isNonEmptyString(merchant.id) &&
    isNonEmptyString(merchant.slug) &&
    isNonEmptyString(merchant.name) &&
    isRecord(service) &&
    isNonEmptyString(service.id) &&
    isNonEmptyString(service.slug) &&
    isNonEmptyString(service.name) &&
    isNonEmptyString(service.service_type) &&
    isJsonValue(value.input) &&
    isNonEmptyString(value.input_hash) &&
    isRecord(pricing) &&
    typeof pricing.amount === "number" &&
    Number.isSafeInteger(pricing.amount) &&
    pricing.amount >= 0 &&
    isNonEmptyString(pricing.currency) &&
    isNonEmptyString(pricing.purchase_type) &&
    isRecord(fulfillment) &&
    typeof fulfillment.maximum_seconds === "number" &&
    Number.isSafeInteger(fulfillment.maximum_seconds) &&
    fulfillment.maximum_seconds > 0 &&
    typeof fulfillment.refund_on_failure === "boolean" &&
    isTimestamp(value.issued_at) &&
    isTimestamp(value.expires_at) &&
    isNonEmptyString(value.quote_hash) &&
    (value.state === "active" || value.state === "expired")
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

function detailMessages(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .slice(0, 3)
      .flatMap((item) => {
        if (!isRecord(item)) {
          return [];
        }

        const message = safeMessage(item.message) ?? safeMessage(item.msg);
        return message ? [message] : [];
      });
  }

  if (isRecord(value)) {
    const directMessage = safeMessage(value.message);
    if (directMessage) {
      return [directMessage];
    }

    return detailMessages(value.issues);
  }

  const directMessage = safeMessage(value);
  return directMessage ? [directMessage] : [];
}

function apiErrorMessage(status: number, body: unknown): string {
  const detail = isRecord(body) ? detailMessages(body.detail).join(" ") : "";

  if (status === 404) {
    return "The quote endpoint could not find this service. It may no longer be available. No payment was attempted.";
  }

  if (status === 409) {
    return `This service or merchant is not currently active.${detail ? ` ${detail}` : ""} No payment was attempted.`;
  }

  if (status === 422) {
    return `The input did not match the service's published schema.${detail ? ` ${detail}` : ""} No payment was attempted.`;
  }

  if (status >= 500) {
    return "The quote service is temporarily unavailable. No payment was attempted.";
  }

  return `The quote request failed with HTTP ${status}. No payment was attempted.`;
}

async function requestQuote(
  endpoint: string,
  serviceId: string,
  input: JsonValue,
  signal: AbortSignal,
): Promise<QuoteResponse> {
  const response = await fetch(endpoint, {
    method: "POST",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ service_id: serviceId, input }),
    signal,
  });

  let body: unknown = null;

  try {
    body = await response.json();
  } catch {
    if (response.status === 201) {
      throw new QuoteRequestFailure(
        "The server issued an unreadable quote response. No payment was attempted.",
      );
    }
  }

  if (response.status !== 201) {
    throw new QuoteRequestFailure(apiErrorMessage(response.status, body));
  }

  if (!isQuoteResponse(body)) {
    throw new QuoteRequestFailure(
      "The server returned an unexpected quote response. No payment was attempted.",
    );
  }

  return body;
}

function requestErrorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") {
    return "The request timed out. A quote may still have been issued; submitting again creates a separate quote. No payment was attempted.";
  }

  if (error instanceof TypeError) {
    return "The quote endpoint could not be reached. Confirm FastAPI is running and CORS allows this origin. No payment was attempted.";
  }

  if (error instanceof QuoteRequestFailure) {
    return error.message;
  }

  return "The quote could not be requested. No payment was attempted.";
}

function formatToken(value: string): string {
  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map((word) => {
      if (word.toLowerCase() === "api") {
        return "API";
      }

      return `${word.charAt(0).toUpperCase()}${word.slice(1).toLowerCase()}`;
    })
    .join(" ");
}

function formatPrice(pricing: QuoteResponse["pricing"]): {
  primary: string;
  minorUnits: string;
} {
  const currency = pricing.currency.toUpperCase();
  const amount = integerFormatter.format(pricing.amount);

  if (currency === "INR") {
    const rupees = Math.trunc(pricing.amount / 100);
    const paise = pricing.amount % 100;
    return {
      primary: `₹${integerFormatter.format(rupees)}.${paise.toString().padStart(2, "0")}`,
      minorUnits: `${amount} paise · ${currency} integer minor units`,
    };
  }

  return {
    primary: `${currency} ${amount}`,
    minorUnits: `${amount} ${currency} minor units; no exponent assumed`,
  };
}

function QuoteDisplay({ quote }: { quote: QuoteResponse }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [expiredByTime, setExpiredByTime] = useState(
    () => quote.state === "expired" || Date.now() >= Date.parse(quote.expires_at),
  );

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  useEffect(() => {
    if (quote.state === "expired" || Date.now() >= Date.parse(quote.expires_at)) {
      return;
    }

    let timeout: number;

    const checkExpiry = () => {
      const remaining = Date.parse(quote.expires_at) - Date.now();
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
  }, [quote.expires_at, quote.state]);

  const displayState =
    quote.state === "expired" || expiredByTime ? "expired" : "active";
  const price = formatPrice(quote.pricing);

  return (
    <section className="mt-5 rounded-2xl border border-cyan-300/15 bg-cyan-300/[0.035] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-cyan-300/75">
            Server-issued quote
          </p>
          <h5
            ref={headingRef}
            tabIndex={-1}
            className="mt-2 text-base font-semibold text-white focus:outline-none"
          >
            Quote details
          </h5>
        </div>
        <span
          className={`rounded-full border px-2.5 py-1 text-[11px] font-medium ${
            displayState === "active"
              ? "border-emerald-300/15 bg-emerald-300/[0.06] text-emerald-200"
              : "border-amber-300/15 bg-amber-300/[0.06] text-amber-100"
          }`}
        >
          {formatToken(displayState)}
        </span>
      </div>

      <p className="mt-3 rounded-xl border border-white/[0.07] bg-black/15 px-3 py-2.5 text-xs leading-5 text-slate-300">
        This quote reserves no funds and does not authorize or execute payment.
      </p>

      <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2">
        <div>
          <dt className="text-slate-500">Quote ID</dt>
          <dd className="mt-1 break-all font-mono text-slate-300">{quote.id}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Derived state</dt>
          <dd className="mt-1 text-slate-300">
            {formatToken(displayState)} · based on server expiry
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Merchant</dt>
          <dd className="mt-1 text-slate-200">{quote.merchant.name}</dd>
          <dd className="mt-1 break-all font-mono text-[10px] text-slate-500">
            {quote.merchant.slug} · {quote.merchant.id}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Service</dt>
          <dd className="mt-1 text-slate-200">{quote.service.name}</dd>
          <dd className="mt-1 break-all text-[10px] text-slate-500">
            {formatToken(quote.service.service_type)} · {quote.service.slug} ·{" "}
            <span className="font-mono">{quote.service.id}</span>
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Server price</dt>
          <dd className="mt-1 text-base font-semibold text-slate-100">
            {price.primary}
          </dd>
          <dd className="mt-1 text-[10px] text-slate-500">{price.minorUnits}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Purchase type</dt>
          <dd className="mt-1 text-slate-300">
            {formatToken(quote.pricing.purchase_type)}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Issued</dt>
          <dd className="mt-1 text-slate-300">
            <time dateTime={quote.issued_at} title={quote.issued_at}>
              {dateTimeFormatter.format(new Date(quote.issued_at))}
            </time>
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Expires</dt>
          <dd className="mt-1 text-slate-300">
            <time dateTime={quote.expires_at} title={quote.expires_at}>
              {dateTimeFormatter.format(new Date(quote.expires_at))}
            </time>
          </dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-slate-500">Fulfillment terms</dt>
          <dd className="mt-1 text-slate-300">
            Maximum {integerFormatter.format(quote.fulfillment.maximum_seconds)}{" "}
            seconds · refund-on-failure term{" "}
            {quote.fulfillment.refund_on_failure ? "enabled" : "not enabled"}
          </dd>
        </div>
      </dl>

      <div className="mt-4">
        <p className="text-xs text-slate-500">Bound input</p>
        <pre className="mt-2 max-h-52 overflow-auto rounded-xl border border-white/[0.06] bg-black/20 p-3 font-mono text-[11px] leading-5 text-slate-300">
          {JSON.stringify(quote.input, null, 2)}
        </pre>
      </div>

      <dl className="mt-4 space-y-3 text-xs">
        <div>
          <dt className="text-slate-500">Input hash</dt>
          <dd className="mt-1 break-all font-mono text-[10px] leading-4 text-slate-400">
            {quote.input_hash}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500">Quote hash</dt>
          <dd className="mt-1 break-all font-mono text-[10px] leading-4 text-slate-400">
            {quote.quote_hash}
          </dd>
        </div>
      </dl>
    </section>
  );
}

export function ServiceQuoteRequest({
  endpoint,
  inputSchema,
  serviceId,
  serviceName,
}: {
  endpoint: string;
  inputSchema: Record<string, unknown>;
  serviceId: string;
  serviceName: string;
}) {
  const panelId = useId();
  const textareaId = useId();
  const helpId = useId();
  const errorId = useId();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [editorValue, setEditorValue] = useState("{}");
  const [inputError, setInputError] = useState<string | null>(null);
  const [requestState, setRequestState] = useState<RequestState>({ kind: "idle" });

  useEffect(
    () => () => {
      controllerRef.current?.abort();
      controllerRef.current = null;
    },
    [],
  );

  const isSubmitting = requestState.kind === "submitting";
  const toggleLabel = expanded
    ? "Hide quote request"
    : requestState.kind === "resolved"
      ? "View issued quote"
      : "Request quote";

  const submitQuote = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setInputError(null);

    let parsedInput: unknown;

    try {
      parsedInput = JSON.parse(editorValue) as unknown;
    } catch {
      setInputError(
        "Enter valid JSON. Objects, arrays, strings, numbers, booleans, and null are accepted for server validation.",
      );
      if (requestState.kind === "error") {
        setRequestState({ kind: "idle" });
      }
      textareaRef.current?.focus();
      return;
    }

    if (!isJsonValue(parsedInput)) {
      setInputError(
        "Enter JSON with finite numbers and no more than 64 nested levels so the submitted value can be preserved exactly.",
      );
      if (requestState.kind === "error") {
        setRequestState({ kind: "idle" });
      }
      textareaRef.current?.focus();
      return;
    }

    const input = parsedInput;

    const controller = new AbortController();
    controllerRef.current = controller;
    const timeout = window.setTimeout(
      () => controller.abort(),
      REQUEST_TIMEOUT_MS,
    );
    setRequestState({ kind: "submitting" });

    try {
      const quote = await requestQuote(endpoint, serviceId, input, controller.signal);
      if (controllerRef.current === controller) {
        setRequestState({ kind: "resolved", quote });
      }
    } catch (error: unknown) {
      if (controllerRef.current === controller) {
        setRequestState({ kind: "error", message: requestErrorMessage(error) });
      }
    } finally {
      window.clearTimeout(timeout);
      if (controllerRef.current === controller) {
        controllerRef.current = null;
      }
    }
  };

  return (
    <div className="mt-5 border-t border-white/[0.07] pt-5">
      <button
        type="button"
        aria-controls={panelId}
        aria-expanded={expanded}
        disabled={isSubmitting}
        onClick={() => setExpanded((current) => !current)}
        className="w-full rounded-xl border border-cyan-300/20 bg-cyan-300/[0.07] px-4 py-2.5 text-sm font-medium text-cyan-100 transition hover:border-cyan-300/35 hover:bg-cyan-300/[0.11] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-wait disabled:opacity-60"
      >
        {isSubmitting ? "Requesting quote…" : toggleLabel}
      </button>

      {expanded ? (
        <div id={panelId} className="mt-4">
          <form onSubmit={submitQuote} aria-busy={isSubmitting} noValidate>
            <label
              htmlFor={textareaId}
              className="text-xs font-medium text-slate-200"
            >
              Service input (JSON)
            </label>
            <p id={helpId} className="mt-1 text-[11px] leading-5 text-slate-500">
              Enter any valid JSON value. The server validates it against{" "}
              {serviceName}&apos;s published schema and alone decides all quote
              terms.
            </p>
            <textarea
              ref={textareaRef}
              id={textareaId}
              value={editorValue}
              rows={7}
              required
              spellCheck={false}
              autoCapitalize="none"
              autoCorrect="off"
              aria-describedby={`${helpId}${inputError ? ` ${errorId}` : ""}`}
              aria-invalid={inputError ? true : undefined}
              disabled={isSubmitting}
              onChange={(event) => {
                setEditorValue(event.target.value);
                setInputError(null);
                if (requestState.kind === "error") {
                  setRequestState({ kind: "idle" });
                }
              }}
              className="mt-3 w-full resize-y rounded-xl border border-white/10 bg-black/25 px-3 py-3 font-mono text-xs leading-5 text-slate-200 outline-none transition placeholder:text-slate-700 focus:border-cyan-300/35 focus:ring-2 focus:ring-cyan-300/10 disabled:cursor-wait disabled:opacity-60"
            />

            {inputError ? (
              <p
                id={errorId}
                role="alert"
                className="mt-2 text-xs leading-5 text-rose-200"
              >
                {inputError}
              </p>
            ) : null}

            <details className="mt-3 rounded-xl border border-white/[0.07] bg-white/[0.02] px-3 py-2.5">
              <summary className="cursor-pointer text-xs font-medium text-slate-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200">
                View published input schema
              </summary>
              <pre className="mt-3 max-h-52 overflow-auto border-t border-white/[0.06] pt-3 font-mono text-[10px] leading-4 text-slate-500">
                {JSON.stringify(inputSchema, null, 2)}
              </pre>
            </details>

            <button
              type="submit"
              disabled={isSubmitting}
              className="mt-3 w-full rounded-xl border border-white/10 bg-white/[0.045] px-4 py-2.5 text-sm font-medium text-slate-200 transition hover:border-cyan-300/25 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-200 disabled:cursor-wait disabled:opacity-60"
            >
              {isSubmitting ? "Requesting server quote…" : "Submit quote request"}
            </button>
          </form>

          {isSubmitting ? (
            <p role="status" className="mt-3 text-xs leading-5 text-slate-400">
              Requesting immutable quote terms from MeterGate. No payment is being
              attempted.
            </p>
          ) : null}

          {requestState.kind === "error" ? (
            <p
              role="alert"
              className="mt-3 rounded-xl border border-rose-300/10 bg-rose-300/[0.05] px-3 py-2.5 text-xs leading-5 text-rose-100/80"
            >
              {requestState.message}
            </p>
          ) : null}

          {requestState.kind === "resolved" ? (
            <QuoteDisplay key={requestState.quote.id} quote={requestState.quote} />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
