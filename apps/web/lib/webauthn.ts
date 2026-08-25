"use client";

export type SerializedRegistrationCredential = {
  id: string;
  rawId: string;
  response: {
    attestationObject: string;
    clientDataJSON: string;
    transports: string[];
  };
  type: string;
  authenticatorAttachment: string | null;
  clientExtensionResults: AuthenticationExtensionsClientOutputs;
};

export type SerializedAuthenticationCredential = {
  id: string;
  rawId: string;
  response: {
    authenticatorData: string;
    clientDataJSON: string;
    signature: string;
    userHandle: string | null;
  };
  type: string;
  authenticatorAttachment: string | null;
  clientExtensionResults: AuthenticationExtensionsClientOutputs;
};

export type WebAuthnBrowserFailureKind =
  | "cancelled"
  | "invalid_options"
  | "invalid_response"
  | "unsupported"
  | "unavailable";

export class WebAuthnBrowserFailure extends Error {
  constructor(
    readonly kind: WebAuthnBrowserFailureKind,
    message: string,
  ) {
    super(message);
    this.name = "WebAuthnBrowserFailure";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new WebAuthnBrowserFailure(
      "invalid_options",
      `The server returned invalid ${label}.`,
    );
  }

  return value;
}

function requireString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new WebAuthnBrowserFailure(
      "invalid_options",
      `The server returned an invalid ${label}.`,
    );
  }

  return value;
}

function base64UrlToArrayBuffer(value: string, label: string): ArrayBuffer {
  try {
    const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
    const padding = "=".repeat((4 - (normalized.length % 4)) % 4);
    const binary = window.atob(`${normalized}${padding}`);
    const bytes = new Uint8Array(binary.length);

    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }

    return bytes.buffer;
  } catch {
    throw new WebAuthnBrowserFailure(
      "invalid_options",
      `The server returned an invalid ${label}.`,
    );
  }
}

function arrayBufferToBase64Url(value: ArrayBuffer): string {
  const bytes = new Uint8Array(value);
  let binary = "";

  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }

  return window
    .btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/u, "");
}

function credentialDescriptors(
  value: unknown,
  label: string,
): PublicKeyCredentialDescriptor[] | undefined {
  if (value === undefined) {
    return undefined;
  }

  if (!Array.isArray(value)) {
    throw new WebAuthnBrowserFailure(
      "invalid_options",
      `The server returned invalid ${label}.`,
    );
  }

  return value.map((item, index) => {
    const descriptor = requireRecord(item, `${label}[${index}]`);
    return {
      ...descriptor,
      id: base64UrlToArrayBuffer(
        requireString(descriptor.id, `${label}[${index}].id`),
        `${label}[${index}].id`,
      ),
    } as PublicKeyCredentialDescriptor;
  });
}

function creationOptionsFromJson(
  value: unknown,
): PublicKeyCredentialCreationOptions {
  const options = requireRecord(value, "passkey registration options");
  const user = requireRecord(options.user, "passkey registration user");

  return {
    ...options,
    challenge: base64UrlToArrayBuffer(
      requireString(options.challenge, "registration challenge"),
      "registration challenge",
    ),
    user: {
      ...user,
      id: base64UrlToArrayBuffer(
        requireString(user.id, "registration user ID"),
        "registration user ID",
      ),
    },
    excludeCredentials: credentialDescriptors(
      options.excludeCredentials,
      "excluded credentials",
    ),
  } as unknown as PublicKeyCredentialCreationOptions;
}

function requestOptionsFromJson(
  value: unknown,
): PublicKeyCredentialRequestOptions {
  const options = requireRecord(value, "passkey authentication options");

  return {
    ...options,
    challenge: base64UrlToArrayBuffer(
      requireString(options.challenge, "authentication challenge"),
      "authentication challenge",
    ),
    allowCredentials: credentialDescriptors(
      options.allowCredentials,
      "allowed credentials",
    ),
  } as unknown as PublicKeyCredentialRequestOptions;
}

function ensureWebAuthnAvailable(): void {
  if (
    typeof window === "undefined" ||
    typeof navigator === "undefined" ||
    typeof window.PublicKeyCredential === "undefined" ||
    !navigator.credentials ||
    typeof navigator.credentials.create !== "function" ||
    typeof navigator.credentials.get !== "function"
  ) {
    throw new WebAuthnBrowserFailure(
      "unsupported",
      "This browser does not support passkeys through WebAuthn.",
    );
  }

  if (!window.isSecureContext) {
    throw new WebAuthnBrowserFailure(
      "unsupported",
      "Passkeys require a secure browser context. Use HTTPS or localhost.",
    );
  }
}

function translateBrowserError(error: unknown): never {
  if (error instanceof WebAuthnBrowserFailure) {
    throw error;
  }

  if (error instanceof DOMException && error.name === "NotAllowedError") {
    throw new WebAuthnBrowserFailure(
      "cancelled",
      "The passkey ceremony was cancelled or was not completed in time.",
    );
  }

  if (error instanceof DOMException && error.name === "InvalidStateError") {
    throw new WebAuthnBrowserFailure(
      "unavailable",
      "The authenticator could not use this credential in its current state.",
    );
  }

  throw new WebAuthnBrowserFailure(
    "unavailable",
    "The browser could not complete the passkey ceremony.",
  );
}

export function passkeySupportError(): string | null {
  try {
    ensureWebAuthnAvailable();
    return null;
  } catch (error: unknown) {
    if (error instanceof WebAuthnBrowserFailure) {
      return error.message;
    }

    return "This browser cannot use passkeys.";
  }
}

export async function createPasskeyCredential(
  options: unknown,
): Promise<SerializedRegistrationCredential> {
  try {
    ensureWebAuthnAvailable();
    const credential = await navigator.credentials.create({
      publicKey: creationOptionsFromJson(options),
    });

    if (!(credential instanceof PublicKeyCredential)) {
      throw new WebAuthnBrowserFailure(
        "invalid_response",
        "The browser returned an unexpected passkey registration response.",
      );
    }

    const response = credential.response;
    if (!(response instanceof AuthenticatorAttestationResponse)) {
      throw new WebAuthnBrowserFailure(
        "invalid_response",
        "The browser returned an unexpected passkey attestation response.",
      );
    }

    return {
      id: credential.id,
      rawId: arrayBufferToBase64Url(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment,
      clientExtensionResults: credential.getClientExtensionResults(),
      response: {
        attestationObject: arrayBufferToBase64Url(response.attestationObject),
        clientDataJSON: arrayBufferToBase64Url(response.clientDataJSON),
        transports:
          typeof response.getTransports === "function"
            ? response.getTransports()
            : [],
      },
    };
  } catch (error: unknown) {
    return translateBrowserError(error);
  }
}

export async function getPasskeyCredential(
  options: unknown,
): Promise<SerializedAuthenticationCredential> {
  try {
    ensureWebAuthnAvailable();
    const credential = await navigator.credentials.get({
      publicKey: requestOptionsFromJson(options),
    });

    if (!(credential instanceof PublicKeyCredential)) {
      throw new WebAuthnBrowserFailure(
        "invalid_response",
        "The browser returned an unexpected passkey authentication response.",
      );
    }

    const response = credential.response;
    if (!(response instanceof AuthenticatorAssertionResponse)) {
      throw new WebAuthnBrowserFailure(
        "invalid_response",
        "The browser returned an unexpected passkey assertion response.",
      );
    }

    return {
      id: credential.id,
      rawId: arrayBufferToBase64Url(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment,
      clientExtensionResults: credential.getClientExtensionResults(),
      response: {
        authenticatorData: arrayBufferToBase64Url(response.authenticatorData),
        clientDataJSON: arrayBufferToBase64Url(response.clientDataJSON),
        signature: arrayBufferToBase64Url(response.signature),
        userHandle: response.userHandle
          ? arrayBufferToBase64Url(response.userHandle)
          : null,
      },
    };
  } catch (error: unknown) {
    return translateBrowserError(error);
  }
}
