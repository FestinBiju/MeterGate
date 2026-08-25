"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";

import {
  ApiRequestFailure,
  configuredApiBaseEndpoint,
  requestCredentialedJson,
} from "@/lib/api-client";

export type MeterGateAccount = {
  id: string;
  display_name: string;
  status: "active";
  created_at: string;
  updated_at: string;
};

export type AccountApprovalIdentity = {
  id: string;
  status: "active";
  credential_count: number;
};

export type AuthenticatedSession = {
  reason_code: "AUTH_LOGGED_IN";
  account: MeterGateAccount;
  approval_identity: AccountApprovalIdentity;
  auth_method: "passkey";
  authenticated_at: string;
  expires_at: string;
};

export type AccountFailure = {
  code: string;
  message: string;
};

export type AccountSessionState =
  | { kind: "checking" }
  | { kind: "anonymous"; failure?: AccountFailure }
  | { kind: "authenticated"; session: AuthenticatedSession };

export type AuthenticatedRequestOptions = {
  method: "GET" | "POST";
  body?: Record<string, unknown>;
  signal?: AbortSignal;
  allowEmptyResponse?: boolean;
};

export type AuthenticatedRequester = (
  endpoint: string,
  options: AuthenticatedRequestOptions,
) => Promise<Record<string, unknown>>;

type InternalAuthenticatedSession = AuthenticatedSession & {
  csrfToken: string;
};

type AccountSessionContextValue = {
  apiBaseEndpoint: string | null;
  state: AccountSessionState;
  sessionRevision: number;
  establishSession: (value: unknown) => AuthenticatedSession;
  clearSession: (failure?: AccountFailure) => void;
  refreshSession: () => Promise<AuthenticatedSession | null>;
  requestAuthenticated: AuthenticatedRequester;
};

const AccountSessionContext = createContext<AccountSessionContextValue | null>(
  null,
);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isTimestamp(value: unknown): value is string {
  return isNonEmptyString(value) && Number.isFinite(Date.parse(value));
}

function parseSession(value: unknown): InternalAuthenticatedSession | null {
  if (!isRecord(value)) {
    return null;
  }

  const account = value.account;
  const approvalIdentity = value.approval_identity;
  if (
    value.reason_code !== "AUTH_LOGGED_IN" ||
    value.auth_method !== "passkey" ||
    !isTimestamp(value.authenticated_at) ||
    !isTimestamp(value.expires_at) ||
    !isNonEmptyString(value.csrf_token) ||
    !isRecord(account) ||
    !isNonEmptyString(account.id) ||
    !isNonEmptyString(account.display_name) ||
    account.status !== "active" ||
    !isTimestamp(account.created_at) ||
    !isTimestamp(account.updated_at) ||
    !isRecord(approvalIdentity) ||
    !isNonEmptyString(approvalIdentity.id) ||
    approvalIdentity.status !== "active" ||
    typeof approvalIdentity.credential_count !== "number" ||
    !Number.isSafeInteger(approvalIdentity.credential_count) ||
    approvalIdentity.credential_count < 1
  ) {
    return null;
  }

  return {
    reason_code: "AUTH_LOGGED_IN",
    account: {
      id: account.id,
      display_name: account.display_name,
      status: "active",
      created_at: account.created_at,
      updated_at: account.updated_at,
    },
    approval_identity: {
      id: approvalIdentity.id,
      status: "active",
      credential_count: approvalIdentity.credential_count,
    },
    auth_method: "passkey",
    authenticated_at: value.authenticated_at,
    expires_at: value.expires_at,
    csrfToken: value.csrf_token,
  };
}

function publicSession(
  session: InternalAuthenticatedSession,
): AuthenticatedSession {
  return {
    reason_code: session.reason_code,
    account: session.account,
    approval_identity: session.approval_identity,
    auth_method: session.auth_method,
    authenticated_at: session.authenticated_at,
    expires_at: session.expires_at,
  };
}

function sessionFailure(error: ApiRequestFailure): AccountFailure | undefined {
  if (error.code === "AUTH_SESSION_REQUIRED") {
    return undefined;
  }

  return { code: error.code, message: error.message };
}

export function AccountSessionProvider({ children }: { children: ReactNode }) {
  const apiBaseEndpoint = useMemo(() => configuredApiBaseEndpoint(), []);
  const internalSessionRef = useRef<InternalAuthenticatedSession | null>(null);
  const [state, setState] = useState<AccountSessionState>(() =>
    apiBaseEndpoint
      ? { kind: "checking" }
      : {
          kind: "anonymous",
          failure: {
            code: "API_NOT_CONFIGURED",
            message:
              "NEXT_PUBLIC_API_URL is not configured. Set it to the FastAPI origin and restart Next.js.",
          },
        },
  );
  const [sessionRevision, setSessionRevision] = useState(0);

  const establishSession = useCallback((value: unknown) => {
    const parsed = parseSession(value);
    if (!parsed) {
      throw new ApiRequestFailure(
        "AUTH_SESSION_RESPONSE_INVALID",
        "The authentication service returned an unexpected session response.",
      );
    }

    if (Date.now() >= Date.parse(parsed.expires_at)) {
      throw new ApiRequestFailure(
        "AUTH_SESSION_EXPIRED",
        "The authenticated session had already expired. Sign in again.",
        401,
      );
    }

    internalSessionRef.current = parsed;
    const safeSession = publicSession(parsed);
    setState({ kind: "authenticated", session: safeSession });
    setSessionRevision((current) => current + 1);
    return safeSession;
  }, []);

  const clearSession = useCallback((failure?: AccountFailure) => {
    internalSessionRef.current = null;
    setState({ kind: "anonymous", failure });
    setSessionRevision((current) => current + 1);
  }, []);

  const refreshSession = useCallback(async () => {
    if (!apiBaseEndpoint) {
      clearSession({
        code: "API_NOT_CONFIGURED",
        message:
          "NEXT_PUBLIC_API_URL is not configured. Set it to the FastAPI origin and restart Next.js.",
      });
      return null;
    }

    try {
      const body = await requestCredentialedJson(
        `${apiBaseEndpoint}/auth/session`,
        { method: "GET" },
      );
      return establishSession(body);
    } catch (error: unknown) {
      if (error instanceof ApiRequestFailure && error.status === 401) {
        clearSession(sessionFailure(error));
        return null;
      }
      throw error;
    }
  }, [apiBaseEndpoint, clearSession, establishSession]);

  useEffect(() => {
    let active = true;

    if (!apiBaseEndpoint) {
      return () => {
        active = false;
      };
    }

    requestCredentialedJson(`${apiBaseEndpoint}/auth/session`, {
      method: "GET",
    })
      .then((body) => {
        if (active) {
          establishSession(body);
        }
      })
      .catch((error: unknown) => {
        if (!active) {
          return;
        }

        if (error instanceof ApiRequestFailure) {
          clearSession(
            error.status === 401
              ? sessionFailure(error)
              : { code: error.code, message: error.message },
          );
          return;
        }

        clearSession({
          code: "AUTH_SESSION_INVALID",
          message: "The current account session could not be checked.",
        });
      });

    return () => {
      active = false;
    };
  }, [apiBaseEndpoint, clearSession, establishSession]);

  useEffect(() => {
    if (state.kind !== "authenticated") {
      return;
    }

    let timeout: number;
    const checkExpiry = () => {
      const remaining = Date.parse(state.session.expires_at) - Date.now();
      if (remaining <= 0) {
        clearSession({
          code: "AUTH_SESSION_EXPIRED",
          message: "Your account session expired. Sign in to continue.",
        });
        return;
      }

      timeout = window.setTimeout(checkExpiry, Math.min(remaining, 60_000));
    };

    checkExpiry();
    return () => window.clearTimeout(timeout);
  }, [clearSession, state]);

  const requestAuthenticated = useCallback(
    async (endpoint: string, options: AuthenticatedRequestOptions) => {
      if (!apiBaseEndpoint) {
        throw new ApiRequestFailure(
          "API_NOT_CONFIGURED",
          "NEXT_PUBLIC_API_URL is not configured.",
        );
      }

      let requestedUrl: URL;
      let apiUrl: URL;
      try {
        requestedUrl = new URL(endpoint);
        apiUrl = new URL(apiBaseEndpoint);
      } catch {
        throw new ApiRequestFailure(
          "API_ENDPOINT_INVALID",
          "The authenticated API endpoint is invalid.",
        );
      }
      if (
        requestedUrl.origin !== apiUrl.origin ||
        !requestedUrl.pathname.startsWith(`${apiUrl.pathname}/`)
      ) {
        throw new ApiRequestFailure(
          "API_ENDPOINT_INVALID",
          "Authenticated requests are restricted to the configured MeterGate API.",
        );
      }

      const session = internalSessionRef.current;
      if (!session) {
        throw new ApiRequestFailure(
          "AUTH_SESSION_REQUIRED",
          "Sign in with a passkey to continue.",
          401,
        );
      }

      try {
        return await requestCredentialedJson(endpoint, {
          ...options,
          csrfToken: options.method === "POST" ? session.csrfToken : undefined,
        });
      } catch (error: unknown) {
        if (error instanceof ApiRequestFailure && error.status === 401) {
          clearSession({ code: error.code, message: error.message });
        }
        throw error;
      }
    },
    [apiBaseEndpoint, clearSession],
  );

  const value = useMemo<AccountSessionContextValue>(
    () => ({
      apiBaseEndpoint,
      state,
      sessionRevision,
      establishSession,
      clearSession,
      refreshSession,
      requestAuthenticated,
    }),
    [
      apiBaseEndpoint,
      clearSession,
      establishSession,
      refreshSession,
      requestAuthenticated,
      sessionRevision,
      state,
    ],
  );

  return (
    <AccountSessionContext.Provider value={value}>
      {children}
    </AccountSessionContext.Provider>
  );
}

export function useAccountSession(): AccountSessionContextValue {
  const context = useContext(AccountSessionContext);
  if (!context) {
    throw new Error("useAccountSession must be used inside AccountSessionProvider");
  }
  return context;
}
