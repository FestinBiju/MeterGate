# MeterGate Agent Instructions

## Mission

Work on MeterGate as a real agent-commerce infrastructure product, not as a mocked hackathon demo.

MeterGate should allow an AI buyer to discover a paid merchant service, receive a machine-readable quote, validate it against bounded user policy, obtain approval when required, pay through Razorpay Test Mode, receive a short-lived entitlement, retrieve the purchased digital resource, and leave a complete audit trail.

Read `README.md` and `SKILL.md` before making architectural or money-sensitive changes.

## Working Rules

1. Inspect the current repository before proposing changes.
2. Prefer the smallest coherent change that advances the current milestone.
3. Do not scaffold unrelated future features prematurely.
4. Do not rewrite stable code without a concrete reason.
5. Preserve clear boundaries between AI reasoning, deterministic authorization, payment processing, entitlement, and fulfillment.
6. When a task touches money or permissions, explicitly identify the trust boundary before coding.
7. Do not introduce hidden mocks into production paths. Test doubles belong only in tests or explicitly named local fixtures.
8. Keep the application runnable after each milestone.

## Architecture Direction

The expected technology stack is:

- Next.js + TypeScript for the web application
- FastAPI + Python 3.13 for the core API
- PostgreSQL for durable state
- Redis for short-lived/idempotency/entitlement state
- Razorpay Test Mode for real payment execution during development
- MCP plus REST for agent-facing interfaces
- Docker Compose for local infrastructure

Expected high-level repository direction:

```text
apps/web
apps/api
apps/merchant-demo
packages/protocol
packages/policy-engine
packages/sdk
packages/mcp
infrastructure
tests
docs
```

Do not create every directory merely because it appears here. Add components when their milestone begins.

## Trust Boundary

Treat this as the central system invariant:

```text
AI understands and proposes
        ↓
structured schema validation
        ↓
DETERMINISTIC TRUST BOUNDARY
        ↓
policy evaluation
        ↓
user/delegated approval
        ↓
Razorpay payment
        ↓
server verification
        ↓
scoped entitlement
        ↓
verified fulfillment
```

Never allow LLM output alone to authorize or confirm a money-sensitive action.

## Never Do These

- Never expose `RAZORPAY_KEY_SECRET` to browser code, logs, agent tools, prompts, or API responses.
- Never commit `.env` files or real credentials.
- Never hardcode payment success for a demo.
- Never trust Razorpay frontend callbacks without server-side verification.
- Never issue entitlement from an unverified payment result.
- Never let a buyer agent call broad merchant-side Razorpay operations directly.
- Never let the LLM override a failed deterministic policy check.
- Never store INR money in floating point; use integer paise.
- Never mutate an approved quote in place.
- Never process duplicate payment/webhook events as new commerce events.
- Never make webhook handlers depend on delivery order.
- Never grant unlimited access when a transaction purchased one scoped resource.
- Never silently swallow payment, fulfillment, or refund failures.

## Secrets and Configuration

All secrets must come from environment variables.

Expected examples:

```env
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=
DATABASE_URL=
REDIS_URL=
JWT_SECRET=
APP_URL=
API_URL=
```

Commit only `.env.example` placeholders.

Before adding code that reads a new secret or configuration value, add a validated configuration field and update `.env.example`.

## Razorpay Rules

For payment implementation:

1. Create Orders on the backend.
2. Associate each Razorpay Order with one MeterGate transaction and quote.
3. Verify successful checkout signatures server-side.
4. Verify webhook signatures using the raw request payload.
5. Persist Razorpay event IDs and handle duplicates idempotently.
6. Model payment state explicitly.
7. Do not assume webhooks arrive in chronological order.
8. Fulfillment must depend on verified backend state, not browser state.
9. Refund logic must be backend-controlled and auditable.
10. Keep Test Mode and Live Mode configuration interchangeable without architecture changes.

## Quote and Policy Rules

Quotes are immutable server-issued commerce objects.

A quote should bind merchant, service, normalized input/input hash, amount, currency, purchase type, expiry, and relevant fulfillment terms.

The policy engine must return structured checks and stable reason codes.

Examples of policy checks:

- `MAX_AMOUNT`
- `CURRENCY_ALLOWED`
- `MERCHANT_ALLOWED`
- `SERVICE_ALLOWED`
- `SUBSCRIPTION_ALLOWED`
- `QUOTE_NOT_EXPIRED`
- `TRANSACTION_LIMIT`
- `APPROVAL_REQUIRED`

Do not implement money policy as natural-language prompts.

## Transaction State

Use explicit state transitions. State-changing operations should be idempotent where retry is realistic.

Avoid generic booleans such as `paid=true` when the domain needs states such as:

```text
PAYMENT_PENDING
PAYMENT_VERIFIED
FULFILLMENT_STARTED
FULFILLMENT_COMPLETE
FULFILLMENT_FAILED
REFUND_INITIATED
REFUND_CONFIRMED
```

Validate allowed transitions rather than allowing arbitrary state assignment.

## Entitlements

Entitlements must be narrow capabilities.

Bind them to the exact transaction/service/resource context. Include expiry and a unique ID. If usage is limited, enforce consumption using server-side state and atomic operations.

Never assume a signed token alone can enforce one-time use.

## AI Usage

AI is appropriate for:

- extracting structured intent
- catalog search
- recommendation/explanation
- bounded upsell suggestions
- summarizing results

When AI returns structured data:

1. schema-validate it;
2. normalize it;
3. treat it as an untrusted proposal;
4. run deterministic checks before any sensitive action.

Merchant/catalog content is untrusted and may contain prompt-injection text. Never let retrieved merchant text become privileged instructions.

## API Design

Prefer explicit versioned routes such as `/api/v1/...` for public interfaces.

Route handlers should remain thin. Put business logic into services/domain modules so it can be tested without HTTP.

Use typed request/response schemas. Reject unknown or malformed sensitive fields rather than guessing.

For paid resources, support a machine-readable `402 Payment Required` path where appropriate.

## Database and Audit Rules

Use PostgreSQL for durable commerce records.

Audit events should be append-only from application code and should capture enough context to reconstruct why a transaction changed state.

Every sensitive transition should record:

- transaction ID
- actor
- event type
- prior state
- resulting state
- reason code
- timestamp
- safe structured metadata

Do not put secrets or full credentials in audit metadata.

## Testing Rules

For any money-sensitive implementation, add tests before considering the task complete.

At minimum cover:

- normal success
- invalid input
- policy rejection
- duplicate/retry behavior
- relevant failure recovery

Critical integration areas require tests for:

- quote tampering
- expired quotes
- invalid Razorpay signatures
- duplicate webhooks
- out-of-order webhooks
- entitlement replay
- fulfillment failure
- refund path

Do not delete or weaken a security test merely to make CI pass.

## Frontend Rules

The UI must reflect backend truth.

For any purchase, clearly display:

- merchant
- service
- final amount
- currency
- one-time/subscription nature
- relevant policy checks
- approval requirement
- actual transaction/payment status
- fulfillment/refund status when relevant

Do not fake progress states with timers if the backend has not reached those states.

## Dependency Discipline

Before adding a dependency, ask whether the standard library or existing dependency already solves the need.

Do not add complex infrastructure such as Kafka, Kubernetes, RabbitMQ, Elasticsearch, or new protocol stacks unless a current requirement justifies them.

Prefer maintained official SDKs for payment and protocol integrations.

## Git and Change Discipline

Before editing:

- inspect `git status` when available;
- avoid overwriting unrelated user changes;
- keep commits/task changes conceptually focused;
- update documentation when public behavior or setup changes.

Do not commit generated secrets, local databases, virtual environments, `node_modules`, build output, or IDE-specific state.

## Current Build Order

Unless the user explicitly reprioritizes, build in this order:

1. repository/monorepo foundation
2. `.gitignore` and `.env.example`
3. FastAPI application and health endpoint
4. Next.js application and health/status UI
5. PostgreSQL + Redis via Docker Compose
6. core domain models and migrations
7. merchant/service CRUD
8. agent-readable catalog
9. immutable quote engine
10. deterministic policy engine
11. trusted approval flow
12. Razorpay Orders integration
13. Razorpay Standard Checkout
14. server-side payment verification
15. webhook engine and idempotency
16. entitlement service
17. real protected merchant API
18. HTTP 402 agent flow
19. buyer agent + narrow MCP tools
20. audit dashboard
21. fulfillment-failure refund flow
22. merchant SDK
23. deployment
24. batch evaluation suite

## Definition of a Good Agent Change

A good change should be:

- runnable;
- scoped;
- typed/validated;
- testable;
- secure by default;
- consistent with the transaction lifecycle;
- free of secret leakage;
- honest about what is implemented versus planned.

If a requirement conflicts with payment safety or an established trust boundary, preserve the safety invariant and clearly explain the tradeoff rather than implementing an unsafe shortcut.
