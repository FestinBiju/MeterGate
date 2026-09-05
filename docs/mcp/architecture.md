# MeterGate MCP architecture

MeterGate's local MCP process is a narrow stdio adapter built with the official Model Context Protocol TypeScript SDK. It contains no payment, policy, entitlement, refund, or fulfillment state machine.

```text
MCP-capable client
  -> packages/mcp (stdio; strict tool schemas)
  -> POST /api/v1/mcp/tools/* (short-lived bridge bearer)
  -> existing MeterGate application services
  -> PostgreSQL / Redis / Razorpay Test Mode / OrbitIntel
```

The buyer signs in to the web application using the normal HttpOnly session cookie and creates an Agent Session with a bounded access grant. MeterGate returns its `mcp_…` credential once, stores only a SHA-256 hash, and records account, scope, expiry, last-use, and revocation metadata. The MCP process receives that separate credential through its environment. It never receives the browser cookie or CSRF secret.

The configured credential persists across conversations. After expiry, the owner renews the existing grant in the browser with an action-bound passkey proof; the same MCP process and token then work again. Expired tokens cannot renew themselves, scopes cannot be expanded by renewal, and revocation is permanent. Renewal and one-use proof consumption share one database commit and leave append-only evidence. No extra MCP tool or payment authority is added.

The bridge authenticates orchestration calls, not purchase approval. An `ALLOW` evaluation produces a browser deep link. The owning user opens that link, re-enters the trusted browser boundary, approves the exact review through WebAuthn, and completes Razorpay Test Mode Checkout there. MCP can read the resulting state but cannot create an authorization, order, captured payment, refund, incident decision, or operator action.

Every MCP tool delegates to the same catalog, quote, policy, evaluation, payment, entitlement, capability, protected-resource, fulfillment, compensation, and refund evidence used by the HTTP UI. Opaque IDs remain locators; account ownership is enforced again by those services. Capability validation and compensation quarantine are therefore unchanged.

Stdio stdout is reserved for MCP protocol frames. Readiness and safe process diagnostics go to stderr. Tool outputs include structured data and a short explanation; neither the bridge token nor capability is written to server logs or the MCP audit table.
