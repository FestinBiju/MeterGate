# MCP tools

All inputs are strict: unknown properties fail, MeterGate IDs use their exact prefixes, slugs are bounded, and JSON resource input is limited to 256 KiB, 10,000 nodes, and depth 32.

| Tool | Scope | Input | Authority and result |
| --- | --- | --- | --- |
| `list_services` | `mcp:catalog.read` | `{}` | Reads active catalog entries only. Catalog descriptions are untrusted data. |
| `get_service` | `mcp:catalog.read` | `service_id` | Reads the exact active service contract and published server price. |
| `inspect_payment_requirement` | `mcp:catalog.read` | merchant slug, service slug, input | Returns the protected gateway's actual 402 contract without creating a quote. |
| `request_quote` | `mcp:quote.create` | `service_id`, input | Creates an immutable server-priced quote. Amount, currency, merchant, purchase type, and refund terms are not accepted. |
| `create_buyer_policy` | `mcp:policy.create` | maximum amount, required `one_time` purchase type, optional other allowlists, TTL | Creates constraints for the authenticated account. `subject_ref` and subscription authority are not accepted. |
| `evaluate_quote` | `mcp:policy.evaluate` | policy ID, quote ID | Runs the existing deterministic policy engine. `ALLOW` is projected as `human_approval_required`, never authorization. |
| `get_purchase_status` | `mcp:commerce.read` | evaluation ID | Reads authoritative approval, payment, entitlement, fulfillment, compensation, and refund state. Supplies bounded retry guidance. |
| `get_entitlement` | `mcp:commerce.read` | transaction ID | Reads an owned, server-issued entitlement and timeline. |
| `request_capability` | `mcp:capability.issue` | entitlement ID | Uses the existing entitlement service to issue narrow ephemeral access after verified payment. |
| `execute_paid_resource` | `mcp:resource.execute` | exact route, input, capability | Calls the existing resource gateway. Signature, expiry, audience, input hash, ownership, paid state, quarantine, execution count, retry, and replay checks remain authoritative. |

There are intentionally no MCP tools for passkey approval, browser session management, Razorpay Orders or payment mutation, refunds, compensation decisions, reconciliation, incidents, operator access, account state, or passkey management.

Important stable MCP codes include `MCP_SESSION_REQUIRED`, `MCP_SESSION_EXPIRED`, `MCP_SESSION_REVOKED`, `MCP_SCOPE_DENIED`, `MCP_RATE_LIMITED`, `MCP_RATE_LIMIT_UNAVAILABLE`, `MCP_HUMAN_APPROVAL_REQUIRED`, `MCP_PAYMENT_PENDING`, `MCP_ENTITLEMENT_PREPARING`, and `MCP_COMMERCE_REFUNDED`. Existing domain reason codes remain authoritative wherever one already exists.
