"use client";

type JsonRequestMethod = "GET" | "POST";

type CredentialedJsonRequestOptions = {
  method: JsonRequestMethod;
  body?: Record<string, unknown>;
  csrfToken?: string;
  signal?: AbortSignal;
  timeoutMs?: number;
  allowEmptyResponse?: boolean;
};

export class ApiRequestFailure extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number | null = null,
  ) {
    super(message);
    this.name = "ApiRequestFailure";
  }
}

const DEFAULT_REQUEST_TIMEOUT_MS = 10_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
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
      const message = detailMessage(item);
      if (message) {
        return message;
      }
    }
    return null;
  }

  if (isRecord(value)) {
    return (
      safeMessage(value.message) ??
      safeMessage(value.msg) ??
      detailMessage(value.issues)
    );
  }

  return safeMessage(value);
}

function responseFailure(
  status: number,
  body: unknown,
): ApiRequestFailure {
  const code =
    (isRecord(body) ? safeMessage(body.reason_code) : null) ??
    `API_HTTP_${status}`;
  const detail = isRecord(body) ? detailMessage(body.detail) : null;
  const message =
    detail ??
    (status === 401
      ? "Your authenticated session is unavailable. Sign in to continue."
      : status === 403
        ? "This account is not allowed to perform that operation."
        : status >= 500
          ? "The MeterGate API is temporarily unavailable."
          : `The MeterGate API request failed with HTTP ${status}.`);

  return new ApiRequestFailure(code, message, status);
}

export function configuredApiBaseEndpoint(): string | null {
  const configuredApiUrl = process.env.NEXT_PUBLIC_API_URL?.trim() ?? "";
  return configuredApiUrl
    ? `${configuredApiUrl.replace(/\/+$/, "")}/api/v1`
    : null;
}

export async function requestCredentialedJson(
  endpoint: string,
  options: CredentialedJsonRequestOptions,
): Promise<Record<string, unknown>> {
  const controller = new AbortController();
  let timedOut = false;
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS);
  const abortFromCaller = () => controller.abort();

  if (options.signal?.aborted) {
    controller.abort();
  } else {
    options.signal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  try {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (options.csrfToken) {
      headers["X-CSRF-Token"] = options.csrfToken;
    }

    const response = await fetch(endpoint, {
      method: options.method,
      cache: "no-store",
      credentials: "include",
      headers,
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });

    const text = await response.text();
    let body: unknown = null;
    if (text) {
      try {
        body = JSON.parse(text) as unknown;
      } catch {
        if (response.ok) {
          throw new ApiRequestFailure(
            "API_RESPONSE_INVALID",
            "The MeterGate API returned unreadable JSON.",
            response.status,
          );
        }
      }
    }

    if (!response.ok) {
      throw responseFailure(response.status, body);
    }

    if (!text && options.allowEmptyResponse) {
      return {};
    }

    if (!isRecord(body)) {
      throw new ApiRequestFailure(
        "API_RESPONSE_INVALID",
        "The MeterGate API returned an unexpected response.",
        response.status,
      );
    }

    return body;
  } catch (error: unknown) {
    if (error instanceof ApiRequestFailure) {
      throw error;
    }

    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiRequestFailure(
        timedOut ? "API_REQUEST_TIMEOUT" : "API_REQUEST_CANCELLED",
        timedOut
          ? "The MeterGate API request timed out. Its final server state may be unknown."
          : "The MeterGate API request was cancelled.",
      );
    }

    if (error instanceof TypeError) {
      throw new ApiRequestFailure(
        "API_SERVICE_UNAVAILABLE",
        "The MeterGate API could not be reached. Confirm FastAPI is running and CORS permits this origin.",
      );
    }

    throw new ApiRequestFailure(
      "API_REQUEST_FAILED",
      "The MeterGate API request could not be completed.",
    );
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abortFromCaller);
  }
}
