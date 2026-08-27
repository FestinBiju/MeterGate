# 402-first agent commerce

The showcase begins with only the protected route and exact input:

```http
POST /api/v1/resources/orbitintel/orbital-risk-report/execute
Content-Type: application/json

{"norad_id":25544}
```

Without a capability, the HTTP gateway returns its machine-readable `402 Payment Required` contract. Through MCP, the equivalent `inspect_payment_requirement` tool calls the same projection; it does not fabricate a recipe.

The client then:

1. Reads the returned merchant/service identity and quote recipe.
2. Uses `get_service` if it needs the exact active contract.
3. Calls `request_quote` with only service ID and input. For the seeded report, MeterGate returns the server price ₹5.00 INR and `one_time` purchase type.
4. Creates a policy capped at ₹10.00, restricted to INR, the selected merchant and service, and `one_time`.
5. Calls `evaluate_quote`. MeterGate deterministically checks ₹5 ≤ ₹10 and every allowlist.
6. On `human_approval_required`, displays the deep link and stops.
7. The owning human signs in, reviews exact server evidence, approves with the passkey, and completes Razorpay Test Mode Checkout.
8. The client reads `get_purchase_status` no faster than `retry_after_seconds` until verified payment and entitlement evidence produce `access_ready`.
9. It reads the entitlement, requests a short-lived capability, and calls `execute_paid_resource` with the exact route and input.
10. MeterGate validates all existing gates, invokes OrbitIntel only when it owns the logical execution, stores the bounded result, and returns fresh or replayed evidence.

A historical `paid` transaction can still project `compensation_pending`, `manual_review`, or `refunded`. Those states release no new value and expose no recovery mutation to MCP.

