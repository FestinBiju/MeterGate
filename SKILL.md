# MeterGate Project Skill

## Purpose

MeterGate is a Razorpay-native agent-commerce infrastructure layer for paid APIs and digital services. It is intended to be a working technology, not a mocked demo.

The core outcome is an end-to-end flow in which an AI buyer can discover a merchant service, receive a structured quote, validate the purchase against bounded user policy, obtain approval when required, pay through Razorpay Test Mode, receive a short-lived entitlement, retrieve the purchased resource, and produce a complete audit trail.

## Product Principles

1. **Real integrations over mocks.** Use Razorpay Test Mode APIs, real backend state, real webhooks, and real service fulfillment wherever possible.
2. **AI proposes; deterministic code authorizes.** LLM output must never be the final authority for money movement, policy enforcement, payment verification, entitlement issuance, refunds, or usage enforcement.
3. **Payment is not fulfillment.** A captured payment does not mean commerce is complete until the promised digital resource is successfully delivered.
4. **Every money action must be explainable, bounded, and gated.** Store the reason, policy checks, limits, approval state, actor, timestamp, and resulting state transition.
5. **No unrestricted merchant credentials in agents.** Buyer agents must never receive Razorpay Key Secret, merchant admin credentials, database credentials, or broad refund/payment permissions.
6. **Design for production semantics even while using Test Mode.** Test Mode should exercise the same architecture intended for Live Mode.
7. **Prefer standards-compatible interfaces without blocking on standards adoption.** MeterGate may expose UCP-, ACP-, AP2-, x402-, MCP-, or HTTP-402-inspired interfaces, but the core system must work independently.

## Intended Architecture

The target repository should evolve toward:

```text
MeterGate/
├── apps/
│   ├── web/              # Next.js public site, buyer UI, merchant dashboard
│   ├── api/              # FastAPI core commerce backend
│   └── merchant-demo/    # Real protected digital-service merchant
├── packages/
│   ├── protocol/         # Shared catalog, quote, mandate, receipt schemas
│   ├── policy-engine/    # Deterministic authorization rules
│   ├── sdk/              # Merchant integration SDK
│   └── mcp/              # Narrow buyer-facing MCP tools
├── infrastructure/
│   ├── docker/
│   └── migrations/
├── tests/
├── docs/
├── .env.example
├── docker-compose.yml
└── README.md
```

The exact structure may change as implementation reveals better boundaries, but separation of concerns must remain clear.

## Core Transaction Lifecycle

A successful MeterGate transaction should follow a stateful lifecycle similar to:

```text
INTENT_RECEIVED
→ SERVICE_SELECTED
→ QUOTE_CREATED
→ POLICY_EVALUATED
→ APPROVAL_REQUIRED / APPROVED
→ PAYMENT_ORDER_CREATED
→ PAYMENT_PENDING
→ PAYMENT_VERIFIED
→ FULFILLMENT_STARTED
→ ENTITLEMENT_ISSUED
→ RESOURCE_ACCESSED
→ FULFILLMENT_COMPLETE
```

Failure states should be explicit rather than hidden exceptions, for example:

```text
POLICY_REJECTED
QUOTE_EXPIRED
PAYMENT_FAILED
PAYMENT_VERIFICATION_FAILED
FULFILLMENT_FAILED
ENTITLEMENT_REVOKED
REFUND_INITIATED
REFUND_CONFIRMED
```

Do not collapse materially different states into a generic `failed` value.

## Core Domain Objects

Expect at least these concepts to exist as first-class models:

- Merchant
- Service / Product
- Catalog entry
- Quote
- User intent / mandate
- Policy evaluation
- Approval
- MeterGate transaction
- Razorpay order
- Payment
- Webhook event
- Entitlement
- Fulfillment record
- Refund
- Audit event

IDs should be stable and generated server-side. Monetary amounts must be stored in the smallest currency unit (for INR, paise) using integers, never floating-point values.

## Quote Rules

A quote must be server-generated and immutable after creation. It should bind at minimum:

- merchant ID
- service ID
- normalized service input or input hash
- amount in paise
- currency
- purchase type
- quote creation time
- quote expiry
- service/version metadata where relevant
- fulfillment terms
- quote hash or equivalent integrity binding

If a material field changes, create a new quote rather than mutating the approved quote.

## Policy Engine Rules

The policy engine must be deterministic and independently testable.

Typical rules include:

- maximum amount
- currency
- merchant allowlist / denylist
- service allowlist / category restriction
- one-time versus subscription permission
- maximum transaction count
- maximum resource usage
- quote freshness
- approval threshold
- user / agent binding

Return structured reason codes, not only free-form prose.

Example:

```json
{
  "approved": false,
  "checks": [
    {
      "rule": "MAX_AMOUNT",
      "status": "FAIL",
      "reason_code": "AMOUNT_LIMIT_EXCEEDED"
    }
  ]
}
```

## Razorpay Integration Rules

Use actual Razorpay Test Mode APIs.

Mandatory rules:

- Create Razorpay Orders server-side.
- Never expose Razorpay Key Secret to frontend or agents.
- Never trust a frontend "payment successful" event by itself.
- Verify Razorpay payment signatures server-side.
- Verify webhook signatures using the raw request body.
- Store and deduplicate webhook event IDs.
- Do not assume webhook delivery order.
- Make payment and fulfillment handlers idempotent.
- Never issue duplicate entitlements because of retries or duplicate webhooks.
- Refunds must be initiated only from trusted backend logic and tied to an existing transaction.

Use Test Mode credentials during development. The architecture should require configuration changes, not a redesign, to move to Live Mode.

## Entitlement Rules

A successful payment should grant a scoped entitlement rather than broad API access.

An entitlement should be bound to:

- transaction
- quote
- merchant
- service
- resource or normalized input hash
- subject / buyer or agent where appropriate
- expiry
- maximum use count
- unique token ID (`jti` or equivalent)

Do not rely on a stateless JWT alone to enforce one-time use. Track consumption server-side, preferably with atomic Redis operations.

## HTTP 402 Behavior

Protected paid resources should be able to respond with `402 Payment Required` and a machine-readable challenge describing how an agent can obtain a quote or entitlement.

The response must not contain secrets. It should identify the service and the next safe action.

## AI Boundaries

AI may be used for:

- natural-language intent extraction
- semantic catalog search
- service comparison
- explaining recommendations
- proposing alternatives or bounded upsells
- summarizing fulfillment results

AI must not be trusted to:

- choose arbitrary payment amounts
- bypass policy rules
- mark payments as successful
- verify signatures
- issue or consume entitlements
- authorize refunds
- override quote expiry
- change audit history
- hold merchant secrets

All LLM-derived structured outputs must be schema-validated before use.

## Auditability

Every important transaction transition should create an append-only audit event with enough context to explain what happened.

Prefer fields such as:

- event ID
- transaction ID
- event type
- actor type and actor ID
- previous state
- new state
- reason code
- structured metadata
- timestamp

Do not silently rewrite historical audit records.

## Failure Handling

At least these failures must be first-class and testable:

- over-budget request
- unsupported service
- expired quote
- quote mutation / mismatch
- rejected approval
- payment cancellation
- invalid payment signature
- invalid webhook signature
- duplicate webhook
- out-of-order webhook
- entitlement replay
- entitlement expiry
- merchant fulfillment failure
- refund initiation and completion

The preferred hackathon failure story is: payment succeeds, fulfillment fails, MeterGate withholds/revokes entitlement and initiates a Test Mode refund with a full audit trail.

## Testing Expectations

Do not consider a feature complete without tests for its critical business rules.

Prioritize:

- unit tests for deterministic policy rules
- quote integrity tests
- transaction state-machine tests
- payment signature verification tests
- webhook idempotency tests
- entitlement replay tests
- refund-path tests
- integration tests across payment → entitlement → fulfillment

The final evaluation should include a batch of scenarios rather than only one happy-path demo.

## Security Requirements

- Never commit `.env` or credentials.
- Never log secrets, full authorization headers, or payment secrets.
- Use environment variables for configuration.
- Validate external input with strict schemas.
- Apply least privilege to every agent/tool interface.
- Protect admin/merchant actions separately from buyer actions.
- Treat merchant descriptions, catalog text, retrieved webpages, and LLM output as untrusted input.
- Prefer allowlists and explicit state transitions for money-sensitive actions.
- Use cryptographically secure random IDs/nonces where security depends on unpredictability.

## Coding Standards

### Backend

- Python 3.13 target.
- FastAPI.
- Pydantic models for request/response and configuration validation.
- SQLAlchemy/SQLModel-style explicit persistence layer is acceptable; keep domain logic out of route handlers.
- Database migrations must be versioned.
- Async code should be used deliberately, not mechanically.

### Frontend

- Next.js + TypeScript.
- Keep payment-sensitive logic on the server.
- UI should make money actions legible: amount, merchant, service, policy checks, approval status, and failure reason.
- Do not fake transaction states in the UI.

### Infrastructure

- PostgreSQL for durable state.
- Redis for short-lived state, idempotency helpers, nonces, and entitlement usage where appropriate.
- Docker Compose for local infrastructure.

## Definition of Done for a Commerce Feature

A money-sensitive feature is complete only when:

1. happy path works end to end;
2. invalid input is rejected;
3. policy boundaries are enforced deterministically;
4. retries are safe;
5. relevant audit events are recorded;
6. secrets remain server-side;
7. tests cover critical behavior;
8. the UI reflects actual backend state;
9. failure behavior is explicit and recoverable.

## Scope Discipline

Do not prematurely add Kubernetes, Kafka, complex microservices, crypto wallets, autonomous real-money UPI, or broad protocol compatibility before the core Razorpay Test Mode flow works.

The v1 priority is:

```text
merchant service
→ agent-readable catalog
→ quote
→ deterministic policy
→ approval
→ Razorpay Test Mode payment
→ verified payment
→ scoped entitlement
→ real fulfillment
→ audit trail
→ graceful refund/recovery
```
