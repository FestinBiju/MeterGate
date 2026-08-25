"use client";

import { useEffect, useMemo, useState } from "react";

import { ServiceQuoteRequest } from "@/components/service-quote-request";

type CatalogPricing = {
  amount: number;
  currency: string;
};

type CatalogService = {
  id: string;
  slug: string;
  name: string;
  description: string;
  status: "active";
  service_type: string;
  purchase_type: string;
  pricing: CatalogPricing;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  output_content_type: string;
  maximum_fulfillment_seconds: number;
  refund_on_fulfillment_failure: boolean;
};

type CatalogMerchant = {
  id: string;
  slug: string;
  name: string;
  description: string;
  services: CatalogService[];
};

type CatalogPayload = {
  version: "1";
  generated_at: string;
  merchants: CatalogMerchant[];
};

type CatalogState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "resolved"; payload: CatalogPayload };

type PriceDisplay = {
  primary: string;
  minorUnitContext: string;
};

const REQUEST_TIMEOUT_MS = 8_000;
const integerFormatter = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 0,
});

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isCatalogPricing(value: unknown): value is CatalogPricing {
  return (
    isRecord(value) &&
    typeof value.amount === "number" &&
    Number.isSafeInteger(value.amount) &&
    value.amount >= 0 &&
    isNonEmptyString(value.currency)
  );
}

function isCatalogService(value: unknown): value is CatalogService {
  return (
    isRecord(value) &&
    isNonEmptyString(value.id) &&
    isNonEmptyString(value.slug) &&
    isNonEmptyString(value.name) &&
    isNonEmptyString(value.description) &&
    value.status === "active" &&
    isNonEmptyString(value.service_type) &&
    isNonEmptyString(value.purchase_type) &&
    isCatalogPricing(value.pricing) &&
    isRecord(value.input_schema) &&
    isRecord(value.output_schema) &&
    isNonEmptyString(value.output_content_type) &&
    typeof value.maximum_fulfillment_seconds === "number" &&
    Number.isSafeInteger(value.maximum_fulfillment_seconds) &&
    value.maximum_fulfillment_seconds > 0 &&
    typeof value.refund_on_fulfillment_failure === "boolean"
  );
}

function isCatalogMerchant(value: unknown): value is CatalogMerchant {
  return (
    isRecord(value) &&
    isNonEmptyString(value.id) &&
    isNonEmptyString(value.slug) &&
    isNonEmptyString(value.name) &&
    isNonEmptyString(value.description) &&
    Array.isArray(value.services) &&
    value.services.every(isCatalogService)
  );
}

function isCatalogPayload(value: unknown): value is CatalogPayload {
  return (
    isRecord(value) &&
    value.version === "1" &&
    isNonEmptyString(value.generated_at) &&
    Array.isArray(value.merchants) &&
    value.merchants.every(isCatalogMerchant)
  );
}

function catalogErrorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") {
    return "The catalog request timed out. Confirm the API is running and try again.";
  }

  if (error instanceof TypeError) {
    return "The catalog endpoint could not be reached. Confirm FastAPI is running and CORS allows this origin.";
  }

  if (error instanceof Error) {
    return error.message;
  }

  return "The catalog endpoint could not be reached.";
}

async function fetchCatalog(
  endpoint: string,
  signal: AbortSignal,
): Promise<CatalogPayload> {
  const response = await fetch(endpoint, {
    cache: "no-store",
    headers: { Accept: "application/json" },
    signal,
  });

  if (!response.ok) {
    throw new Error(`The catalog request failed with HTTP ${response.status}.`);
  }

  let body: unknown;

  try {
    body = await response.json();
  } catch {
    throw new Error("The API returned a catalog response that was not valid JSON.");
  }

  if (!isCatalogPayload(body)) {
    throw new Error("The API returned an unexpected catalog payload.");
  }

  return body;
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

function formatPrice(pricing: CatalogPricing): PriceDisplay {
  const currency = pricing.currency.toUpperCase();
  const minorUnits = integerFormatter.format(pricing.amount);

  if (currency === "INR") {
    const rupees = Math.trunc(pricing.amount / 100);
    const paise = pricing.amount % 100;
    return {
      primary: `₹${integerFormatter.format(rupees)}.${paise.toString().padStart(2, "0")}`,
      minorUnitContext: `${minorUnits} paise · integer minor units`,
    };
  }

  return {
    primary: `${currency} ${minorUnits}`,
    minorUnitContext: "Minor units as published; no currency exponent assumed",
  };
}

function CatalogLoading() {
  return (
    <div role="status" className="grid gap-4 lg:grid-cols-2">
      <span className="sr-only">Loading available agent services.</span>
      {[0, 1].map((item) => (
        <div
          key={item}
          aria-hidden="true"
          className="motion-safe:animate-pulse rounded-2xl border border-white/[0.07] bg-white/[0.025] p-6"
        >
          <div className="h-3 w-24 rounded-full bg-white/[0.08]" />
          <div className="mt-5 h-6 w-3/5 rounded-full bg-white/[0.09]" />
          <div className="mt-4 h-3 w-full rounded-full bg-white/[0.06]" />
          <div className="mt-2 h-3 w-4/5 rounded-full bg-white/[0.06]" />
          <div className="mt-8 h-10 rounded-xl bg-white/[0.05]" />
        </div>
      ))}
    </div>
  );
}

function CatalogError({
  canRetry,
  message,
  onRetry,
}: {
  canRetry: boolean;
  message: string;
  onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-start justify-between gap-5 rounded-2xl border border-rose-300/15 bg-rose-300/[0.045] p-6 sm:flex-row sm:items-center"
    >
      <div>
        <p className="text-sm font-semibold text-rose-100">
          Catalog unavailable
        </p>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-rose-100/65">
          {message}
        </p>
      </div>
      {canRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="shrink-0 rounded-xl border border-rose-200/20 bg-rose-100/[0.05] px-4 py-2.5 text-sm font-medium text-rose-50 transition hover:border-rose-200/35 hover:bg-rose-100/[0.1] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-200"
        >
          Try again
        </button>
      ) : null}
    </div>
  );
}

function CatalogEmpty() {
  return (
    <div
      role="status"
      className="rounded-2xl border border-dashed border-white/10 bg-white/[0.02] px-6 py-14 text-center"
    >
      <p className="text-base font-medium text-slate-200">
        No active services are available yet.
      </p>
      <p className="mx-auto mt-2 max-w-lg text-sm leading-6 text-slate-500">
        The catalog endpoint responded successfully, but it did not publish any
        active agent services.
      </p>
    </div>
  );
}

function ServiceCard({
  policyCreateEndpoint,
  policyEvaluationEndpoint,
  quoteEndpoint,
  service,
}: {
  policyCreateEndpoint: string;
  policyEvaluationEndpoint: string;
  quoteEndpoint: string;
  service: CatalogService;
}) {
  const price = formatPrice(service.pricing);

  return (
    <article className="flex self-start flex-col rounded-2xl border border-white/[0.08] bg-slate-950/65 p-5 shadow-xl shadow-black/10 transition hover:border-cyan-300/15 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="rounded-full border border-cyan-300/15 bg-cyan-300/[0.06] px-2.5 py-1 text-[11px] font-medium text-cyan-100/80">
          {formatToken(service.service_type)}
        </span>
        <span className="inline-flex items-center gap-2 text-xs font-medium text-emerald-200/80">
          <span
            aria-hidden="true"
            className="size-1.5 rounded-full bg-emerald-300 shadow-[0_0_0_4px_rgba(110,231,183,0.07)]"
          />
          {formatToken(service.status)}
        </span>
      </div>

      <h4 className="mt-5 text-xl font-semibold tracking-[-0.02em] text-white">
        {service.name}
      </h4>
      <p className="mt-3 flex-1 text-sm leading-6 text-slate-400">
        {service.description}
      </p>

      <div className="mt-6 flex flex-col gap-4 border-t border-white/[0.07] pt-5 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-xl font-semibold tracking-[-0.02em] text-slate-100">
            {price.primary}
          </p>
          <p className="mt-1 text-[11px] leading-4 text-slate-500">
            {price.minorUnitContext}
          </p>
        </div>
        <span className="w-fit rounded-full border border-white/10 bg-white/[0.035] px-3 py-1.5 text-xs font-medium text-slate-300">
          {formatToken(service.purchase_type)}
        </span>
      </div>

      <ServiceQuoteRequest
        endpoint={quoteEndpoint}
        inputSchema={service.input_schema}
        policyCreateEndpoint={policyCreateEndpoint}
        policyEvaluationEndpoint={policyEvaluationEndpoint}
        serviceId={service.id}
        serviceName={service.name}
      />
    </article>
  );
}

function CatalogResults({
  payload,
  policyCreateEndpoint,
  policyEvaluationEndpoint,
  quoteEndpoint,
}: {
  payload: CatalogPayload;
  policyCreateEndpoint: string;
  policyEvaluationEndpoint: string;
  quoteEndpoint: string;
}) {
  const merchants = payload.merchants.filter(
    (merchant) => merchant.services.length > 0,
  );
  const serviceCount = merchants.reduce(
    (total, merchant) => total + merchant.services.length,
    0,
  );

  if (serviceCount === 0) {
    return <CatalogEmpty />;
  }

  return (
    <div className="space-y-8">
      <p className="text-xs text-slate-500" role="status">
        {serviceCount} active {serviceCount === 1 ? "service" : "services"} from{" "}
        {merchants.length} {merchants.length === 1 ? "merchant" : "merchants"}
        . Live API response, catalog version {payload.version}.
      </p>

      {merchants.map((merchant) => (
        <article
          key={merchant.id}
          className="rounded-3xl border border-white/[0.08] bg-white/[0.018] p-4 sm:p-6"
        >
          <header className="px-1 pb-6 sm:flex sm:items-end sm:justify-between sm:gap-8">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-cyan-300/70">
                Merchant
              </p>
              <h3 className="mt-2 text-2xl font-semibold tracking-[-0.03em] text-white">
                {merchant.name}
              </h3>
            </div>
            <p className="mt-3 max-w-xl text-sm leading-6 text-slate-500 sm:mt-0 sm:text-right">
              {merchant.description}
            </p>
          </header>

          <div className="grid gap-4 lg:grid-cols-2">
            {merchant.services.map((service) => (
              <ServiceCard
                key={service.id}
                policyCreateEndpoint={policyCreateEndpoint}
                policyEvaluationEndpoint={policyEvaluationEndpoint}
                quoteEndpoint={quoteEndpoint}
                service={service}
              />
            ))}
          </div>
        </article>
      ))}
    </div>
  );
}

export function ServiceCatalog() {
  const configuredApiUrl = process.env.NEXT_PUBLIC_API_URL?.trim() ?? "";
  const apiOrigin = useMemo(() => {
    if (!configuredApiUrl) {
      return null;
    }

    return configuredApiUrl.replace(/\/+$/, "");
  }, [configuredApiUrl]);
  const endpoint = apiOrigin ? `${apiOrigin}/api/v1/catalog` : null;
  const quoteEndpoint = apiOrigin ? `${apiOrigin}/api/v1/quotes` : null;
  const policyCreateEndpoint = apiOrigin ? `${apiOrigin}/api/v1/policies` : null;
  const policyEvaluationEndpoint = apiOrigin
    ? `${apiOrigin}/api/v1/policy-evaluations`
    : null;
  const [requestNumber, setRequestNumber] = useState(0);
  const [state, setState] = useState<CatalogState>({ kind: "loading" });

  useEffect(() => {
    if (!endpoint) {
      return;
    }

    let active = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(
      () => controller.abort(),
      REQUEST_TIMEOUT_MS,
    );

    fetchCatalog(endpoint, controller.signal)
      .then((payload) => {
        if (active) {
          setState({ kind: "resolved", payload });
        }
      })
      .catch((error: unknown) => {
        if (active) {
          setState({ kind: "error", message: catalogErrorMessage(error) });
        }
      })
      .finally(() => window.clearTimeout(timeout));

    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [endpoint, requestNumber]);

  const displayState: CatalogState = endpoint
    ? state
    : {
        kind: "error",
        message:
          "NEXT_PUBLIC_API_URL is not configured. Set it to the FastAPI origin and restart Next.js.",
      };

  const retry = () => {
    setState({ kind: "loading" });
    setRequestNumber((current) => current + 1);
  };

  return (
    <section
      aria-labelledby="service-catalog-title"
      aria-busy={displayState.kind === "loading"}
      className="mt-24 border-t border-white/[0.06] pt-16 sm:mt-28 sm:pt-20"
    >
      <div className="mb-9 max-w-2xl">
        <p className="text-xs font-semibold uppercase tracking-[0.22em] text-cyan-300">
          Live service discovery
        </p>
        <h2
          id="service-catalog-title"
          className="mt-3 text-3xl font-semibold tracking-[-0.035em] text-white sm:text-4xl"
        >
          Available Agent Services
        </h2>
        <p className="mt-4 text-sm leading-6 text-slate-400 sm:text-base sm:leading-7">
          Active merchant services published by the MeterGate catalog API. Prices
          are displayed from integer minor-unit values supplied by the backend.
        </p>
      </div>

      {displayState.kind === "loading" ? <CatalogLoading /> : null}
      {displayState.kind === "error" ? (
        <CatalogError
          canRetry={endpoint !== null}
          message={displayState.message}
          onRetry={retry}
        />
      ) : null}
      {displayState.kind === "resolved" &&
      quoteEndpoint &&
      policyCreateEndpoint &&
      policyEvaluationEndpoint ? (
        <CatalogResults
          payload={displayState.payload}
          policyCreateEndpoint={policyCreateEndpoint}
          policyEvaluationEndpoint={policyEvaluationEndpoint}
          quoteEndpoint={quoteEndpoint}
        />
      ) : null}
    </section>
  );
}
