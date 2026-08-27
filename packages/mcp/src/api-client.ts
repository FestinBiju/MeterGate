import { randomUUID } from "node:crypto";

export class MeterGateApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
    readonly retryAfterSeconds?: number,
  ) {
    super(message);
    this.name = "MeterGateApiError";
  }
}

export class MeterGateApiClient {
  readonly baseUrl: URL;

  constructor(
    baseUrl: string,
    private readonly bridgeToken: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {
    this.baseUrl = normalizeBaseUrl(baseUrl);
    if (!/^mcp_[A-Za-z0-9_-]{43}$/.test(bridgeToken)) {
      throw new Error("METERGATE_MCP_TOKEN is missing or malformed");
    }
  }

  async call(path: string, input: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (!/^\/mcp\/tools\/[a-z-]+$/.test(path)) {
      throw new Error("MCP API path is not allowlisted");
    }
    const endpoint = new URL(`.${path}`, this.baseUrl);
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      path.endsWith("execute-paid-resource") ? 70_000 : 20_000,
    );
    let response: Response;
    try {
      response = await this.fetcher(endpoint, {
        method: "POST",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${this.bridgeToken}`,
          "Content-Type": "application/json",
          "X-MCP-Correlation-ID": `mcp-${randomUUID()}`,
        },
        body: JSON.stringify(input),
        signal: controller.signal,
      });
    } catch (error) {
      throw new MeterGateApiError(
        "MCP_API_UNAVAILABLE",
        error instanceof Error && error.name === "AbortError"
          ? "MeterGate did not respond within the bounded timeout."
          : "MeterGate is unavailable.",
        503,
        5,
      );
    } finally {
      clearTimeout(timeout);
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > 1_048_576) {
      throw new MeterGateApiError(
        "MCP_RESPONSE_TOO_LARGE",
        "MeterGate returned an oversized response.",
        502,
      );
    }
    let body: unknown;
    try {
      body = text ? JSON.parse(text) : {};
    } catch {
      throw new MeterGateApiError(
        "MCP_RESPONSE_INVALID",
        "MeterGate returned a non-JSON response.",
        502,
      );
    }
    if (!response.ok) {
      const record = isRecord(body) ? body : {};
      const detail = isRecord(record.detail) ? record.detail : {};
      const code =
        stringValue(record.reason_code) ??
        stringValue(detail.code) ??
        `MCP_HTTP_${response.status}`;
      const message =
        stringValue(record.detail) ??
        "MeterGate rejected the scoped MCP tool call.";
      const retryHeader = response.headers.get("retry-after");
      const parsedRetry = retryHeader ? Number.parseInt(retryHeader, 10) : undefined;
      throw new MeterGateApiError(
        code,
        message,
        response.status,
        parsedRetry && parsedRetry > 0 ? parsedRetry : undefined,
      );
    }
    if (!isRecord(body)) {
      throw new MeterGateApiError(
        "MCP_RESPONSE_INVALID",
        "MeterGate returned an unexpected response shape.",
        502,
      );
    }
    return body;
  }
}

export function clientFromEnvironment(environment = process.env): MeterGateApiClient {
  return new MeterGateApiClient(
    environment.METERGATE_API_URL ?? "http://localhost:8000/api/v1/",
    environment.METERGATE_MCP_TOKEN ?? "",
  );
}

function normalizeBaseUrl(value: string): URL {
  const url = new URL(value);
  const loopback = url.hostname === "localhost" || url.hostname === "127.0.0.1";
  if ((url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) || url.username || url.password) {
    throw new Error("METERGATE_API_URL must be HTTPS or an HTTP loopback URL");
  }
  if (!url.pathname.endsWith("/api/v1/")) {
    throw new Error("METERGATE_API_URL must end with /api/v1/");
  }
  return url;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}
