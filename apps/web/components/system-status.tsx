"use client";

import { useEffect, useMemo, useState } from "react";

type HealthStatus = "ok" | "unavailable";

type ReadinessPayload = {
  status: HealthStatus;
  service: string;
  components: {
    postgresql: { status: HealthStatus };
    redis: { status: HealthStatus };
  };
};

type StatusState =
  | { kind: "loading" }
  | {
      kind: "resolved";
      payload: ReadinessPayload;
      httpStatus: number;
      checkedAt: Date;
    }
  | { kind: "error"; message: string };

type DisplayStatus = {
  label: "Checking" | "Reachable" | "Ready" | "Unavailable";
  tone: "pending" | "positive" | "negative";
};

const REQUEST_TIMEOUT_MS = 6_000;

function hasStatus(value: unknown): value is { status: HealthStatus } {
  if (typeof value !== "object" || value === null || !("status" in value)) {
    return false;
  }

  return value.status === "ok" || value.status === "unavailable";
}

function isReadinessPayload(value: unknown): value is ReadinessPayload {
  if (
    typeof value !== "object" ||
    value === null ||
    !("status" in value) ||
    !("service" in value) ||
    !("components" in value)
  ) {
    return false;
  }

  if (!hasStatus(value) || typeof value.service !== "string") {
    return false;
  }

  const components = value.components;

  return (
    typeof components === "object" &&
    components !== null &&
    "postgresql" in components &&
    "redis" in components &&
    hasStatus(components.postgresql) &&
    hasStatus(components.redis)
  );
}

function displayStatus(
  state: StatusState,
  component?: keyof ReadinessPayload["components"],
): DisplayStatus {
  if (state.kind === "loading") {
    return { label: "Checking", tone: "pending" };
  }

  if (state.kind === "error") {
    return { label: "Unavailable", tone: "negative" };
  }

  if (!component) {
    return { label: "Reachable", tone: "positive" };
  }

  const status = state.payload.components[component].status;

  if (status === "ok") {
    return { label: "Ready", tone: "positive" };
  }

  return { label: "Unavailable", tone: "negative" };
}

function errorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === "AbortError") {
    return "The readiness request timed out. Confirm the API is running and reachable.";
  }

  if (error instanceof Error) {
    if (error instanceof TypeError) {
      return "The readiness endpoint could not be reached. Confirm FastAPI is running and CORS allows this origin.";
    }

    return error.message;
  }

  return "The readiness endpoint could not be reached.";
}

async function fetchReadiness(
  endpoint: string,
  signal: AbortSignal,
): Promise<{ payload: ReadinessPayload; httpStatus: number }> {
  const response = await fetch(endpoint, {
    cache: "no-store",
    headers: { Accept: "application/json" },
    signal,
  });

  let body: unknown;

  try {
    body = await response.json();
  } catch {
    throw new Error(
      `The API returned HTTP ${response.status} without a valid readiness payload.`,
    );
  }

  if (!isReadinessPayload(body)) {
    throw new Error("The API returned an unexpected readiness payload.");
  }

  if (!response.ok && response.status !== 503) {
    throw new Error(`The readiness request failed with HTTP ${response.status}.`);
  }

  return { payload: body, httpStatus: response.status };
}

function StatusIndicator({ tone }: Pick<DisplayStatus, "tone">) {
  const toneClass = {
    pending: "bg-amber-300 shadow-[0_0_0_4px_rgba(252,211,77,0.08)]",
    positive: "bg-emerald-300 shadow-[0_0_0_4px_rgba(110,231,183,0.08)]",
    negative: "bg-rose-300 shadow-[0_0_0_4px_rgba(253,164,175,0.08)]",
  }[tone];

  return <span aria-hidden="true" className={`size-2 rounded-full ${toneClass}`} />;
}

function StatusRow({
  description,
  name,
  status,
}: {
  description: string;
  name: string;
  status: DisplayStatus;
}) {
  return (
    <li className="flex items-center justify-between gap-4 border-b border-white/[0.06] py-4 last:border-0">
      <div className="min-w-0">
        <p className="text-sm font-medium text-slate-100">{name}</p>
        <p className="mt-1 truncate text-xs text-slate-500">{description}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2 text-xs font-medium text-slate-300">
        <StatusIndicator tone={status.tone} />
        {status.label}
      </div>
    </li>
  );
}

export function SystemStatus() {
  const configuredApiUrl = process.env.NEXT_PUBLIC_API_URL?.trim() ?? "";
  const endpoint = useMemo(() => {
    if (!configuredApiUrl) {
      return null;
    }

    return `${configuredApiUrl.replace(/\/+$/, "")}/health/ready`;
  }, [configuredApiUrl]);
  const [requestNumber, setRequestNumber] = useState(0);
  const [state, setState] = useState<StatusState>({ kind: "loading" });

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

    fetchReadiness(endpoint, controller.signal)
      .then(({ payload, httpStatus }) => {
        if (!active) {
          return;
        }

        setState({
          kind: "resolved",
          payload,
          httpStatus,
          checkedAt: new Date(),
        });
      })
      .catch((error: unknown) => {
        if (!active) {
          return;
        }

        setState({ kind: "error", message: errorMessage(error) });
      })
      .finally(() => window.clearTimeout(timeout));

    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [endpoint, requestNumber]);

  const displayState: StatusState = endpoint
    ? state
    : {
        kind: "error",
        message:
          "NEXT_PUBLIC_API_URL is not configured. Set it to the FastAPI origin and restart Next.js.",
      };
  const api = displayStatus(displayState);
  const postgresql = displayStatus(displayState, "postgresql");
  const redis = displayStatus(displayState, "redis");
  const detail =
    displayState.kind === "resolved"
      ? `HTTP ${displayState.httpStatus} · readiness ${displayState.payload.status}`
      : endpoint
        ? "FastAPI readiness endpoint"
        : "API URL not configured";

  return (
    <section
      aria-labelledby="system-status-title"
      className="rounded-3xl border border-white/10 bg-slate-950/70 p-1 shadow-2xl shadow-black/30 backdrop-blur"
    >
      <div className="rounded-[1.3rem] border border-white/[0.06] bg-white/[0.025] p-6 sm:p-7">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">
              Live environment
            </p>
            <h2
              id="system-status-title"
              className="mt-2 text-lg font-semibold text-white"
            >
              Infrastructure readiness
            </h2>
          </div>
          <button
            type="button"
            onClick={() => {
              setState({ kind: "loading" });
              setRequestNumber((current) => current + 1);
            }}
            disabled={displayState.kind === "loading" || !endpoint}
            className="rounded-lg border border-white/10 px-3 py-2 text-xs font-medium text-slate-300 transition hover:border-cyan-300/30 hover:bg-cyan-300/[0.06] hover:text-cyan-100 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {displayState.kind === "loading" ? "Checking…" : "Refresh"}
          </button>
        </div>

        <div
          className="mt-5 overflow-hidden rounded-lg border border-white/[0.06] bg-black/20 px-3 py-2 font-mono text-[11px] text-slate-500"
          title={endpoint ?? undefined}
        >
          <span className="block truncate">
            {endpoint ?? "NEXT_PUBLIC_API_URL/health/ready"}
          </span>
        </div>

        <ul
          className="mt-3"
          aria-live="polite"
          aria-busy={displayState.kind === "loading"}
        >
          <StatusRow name="Backend API" description={detail} status={api} />
          <StatusRow
            name="PostgreSQL"
            description="Durable application state"
            status={postgresql}
          />
          <StatusRow
            name="Redis"
            description="Short-lived infrastructure state"
            status={redis}
          />
        </ul>

        {displayState.kind === "error" ? (
          <p
            role="alert"
            className="mt-4 rounded-xl border border-rose-300/10 bg-rose-300/[0.05] px-4 py-3 text-xs leading-5 text-rose-100/80"
          >
            {displayState.message}
          </p>
        ) : null}

        {displayState.kind === "resolved" ? (
          <p className="mt-4 text-[11px] text-slate-600">
            Checked {displayState.checkedAt.toLocaleTimeString()}. Statuses are
            reported by FastAPI and are not inferred in the browser.
          </p>
        ) : null}
      </div>
    </section>
  );
}
