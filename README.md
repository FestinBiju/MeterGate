# MeterGate

**A Razorpay-native agent storefront for paid APIs and digital services.**

## Presentation entry points

- **Guided demo:** open the homepage and choose successful delivery, budget rejection, or operator-prepared recovery. Each card has a copyable MCP prompt.
- **Delivered value:** the buyer receives a readable OrbitIntel report with source epoch, units, limitations, and expandable raw JSON.
- **Purchase receipt:** download a JSON snapshot from authenticated transaction, authorization, and policy evidence. It is explicitly Test Mode evidence, not a tax invoice or signed attestation.
- **Evidence:** `/evidence` separates recorded policy results from historical provider acceptance. Current private commerce metrics remain in `/operator`.
- [Five-minute presentation and recording checklist](docs/demo/presentation.md)
- [Submission copy and architecture slide](docs/demo/submission-copy.md)
- [Current readiness and live acceptance evidence](docs/demo/readiness-2026-09-05.md)
- [Merchant integration guide](docs/merchant-integration.md)
- [Execution checklist](docs/demo/execution-plan.md)

For a local production frontend build, set `NEXT_PUBLIC_API_URL=http://localhost:8000` in the build process before `npm run build` from `apps/web`. The root `.env` alone does not configure the Next.js build. Use the same `localhost` origin configured for passkeys throughout the demo.

With the local stack running, use `uv run python -m app.scripts.presentation_preflight` from `apps/api`. It checks readiness, the catalog, 402 behavior, anonymous catalog-write rejection, and worker heartbeats without printing credentials. It deliberately does not claim live checkout, webhook delivery, or refund acceptance. Add `--require-refunds` for the configured failure demonstration.

## Track
Razorpay Buildathon — **Track 01: AI Growth & Agentic Commerce**

## Problem Statement
Independent Indian digital-service merchants generally lack a simple, self-serve way to make their APIs, datasets, reports, or other digital services transactable by AI buyers end to end.

Today, an AI agent may be able to discover a service or understand what a merchant offers, but the transaction often breaks at pricing, authorization, payment, access provisioning, fulfillment, or auditability. Emerging standards such as ACP, AP2, UCP, and x402 solve parts of this journey, but merchants still have to connect these pieces themselves and safely integrate them with their payment stack.

This creates a gap between **AI discovery** and **actual completed commerce**.

## Proposed Solution
**MeterGate** turns a merchant's existing API or digital service into an agent-readable, payable product.

An AI buyer can:

1. Discover the merchant's available services.
2. Receive a structured, machine-readable quote.
3. Check the purchase against bounded spending rules.
4. Obtain explicit user approval when required.
5. Pay through Razorpay Test Mode.
6. Receive short-lived access to the purchased resource.
7. Retrieve the result automatically.
8. Generate an auditable transaction and fulfillment trail.

The goal is to help merchants become **discoverable, understandable, payable, and fulfillable by AI agents** without giving those agents unrestricted payment or service access.

## Buildathon Proof

MeterGate deliberately targets the second half of Track 01: making a merchant safely transactable by an AI buyer end to end. That trust and control layer is the revenue-growth precondition for exposing paid merchant APIs to agent traffic.

The seeded agent-readable catalog contains three real service choices at ₹2, ₹5, and ₹9. Codex can interpret a fuzzy request, compare those choices, and propose a service; immutable quotes and deterministic policy remain authoritative. The generated evaluation currently records 60/60 passing scenarios, 0% policy bypass, 0% false blocking, and 0.117 ms policy-evaluation p95. A separate physical acceptance run completed one real Razorpay Test Mode refund from permanent fulfillment failure to provider status `processed`; this is honestly reported as one accepted run, not a population-level recovery claim.

MeterGate provides standards-aligned primitives without claiming protocol conformance: its quote/policy/authorization evidence resembles AP2 mandates, its agent-readable catalog and checkout lifecycle overlap ACP, and its resource-first `402 Payment Required` flow is x402-inspired while deliberately using Razorpay and entitlement capabilities instead of blockchain settlement. See the [five-minute judge demonstration](docs/demo/judge-readiness.md) for exact prompts, evidence, protocol links, and the on-camera failure sequence.

## Current Milestone

Milestone 11 makes the verified commerce loop deployable and measurable. Powerful MCP buyer sessions now require a one-use, action-bound Proof of Human Presence created by `userVerification=required` WebAuthn rather than a visual CAPTCHA. Staging configuration fails closed on insecure cookies/origins or non-Test Razorpay mode; Docker deployment references, CI, secret scanning, guarded demo preparation, sanitized transaction evidence export, and a machine-produced 60-scenario evaluation harness are included. The web presentation now follows the repository's TypeUI Minimal design system while preserving the existing commerce, security, and evidence contracts.

## Agent Interface

`packages/mcp` provides a local stdio server built with the official Model Context Protocol TypeScript SDK. In **Agent Connections**, configure a client once and reuse it across conversations. Access grants last 15 minutes, 30 minutes, or one hour within the server limit. When access expires, choose **Renew access** and verify with your passkey; the existing MCP key and configuration work again. Renewal preserves scopes, binds the proof to the exact connection, and records audit evidence. The agent cannot renew itself. Revocation is permanent, including for expired connections.

MeterGate displays each new secret once and stores only its SHA-256 hash. The credential is separate from the browser's HttpOnly cookie and CSRF secret and never carries operator or passkey authority. A lost or exposed key must be replaced. See [one-time setup and renewal](docs/mcp/client-setup.md).

## MCP Tools

The ten composable tools are `list_services`, `get_service`, `inspect_payment_requirement`, `request_quote`, `create_buyer_policy`, `evaluate_quote`, `get_purchase_status`, `get_entitlement`, `request_capability`, and `execute_paid_resource`. Strict schemas reject unknown fields, money overrides, arbitrary subjects, and oversized input. Each handler delegates to existing application services and emits a secret-free, append-only audit event. See [`docs/mcp/tools.md`](docs/mcp/tools.md).

## 402-first Agent Commerce

An agent may begin with only `POST /api/v1/resources/orbitintel/orbital-risk-report/execute` and `{"norad_id":25544}`. `inspect_payment_requirement` returns that gateway's actual machine-readable `402` contract. The agent follows its service and quote recipe, applies the buyer's deterministic policy, pauses for human approval and Checkout, then observes verified payment and entitlement state before using a narrow capability on the exact resource. See [`docs/mcp/402-first-flow.md`](docs/mcp/402-first-flow.md).

## Human Approval Boundary

MCP has no approval or payment-mutation tool. Policy `ALLOW` returns `MCP_HUMAN_APPROVAL_REQUIRED` and a server-generated `/agent-purchases/{evaluation_id}` link. The owning user opens the trusted web UI, reviews authoritative merchant/service/price/policy evidence, approves through the existing WebAuthn component, and completes Razorpay Test Mode Checkout. The agent waits and only reads subsequent state.

## Reference Buyer Agent

The reference CLI in `packages/mcp` interprets a request such as “Get an orbital risk report for NORAD 25544. Spend no more than ₹10. One-time only,” selects a relevant catalog service, and uses only MeterGate tools for commerce facts. The default planner is deterministic and replaceable with an LLM only for intent/service selection. An adversarial catalog prompt that asks for a ₹999 purchase still reaches deterministic policy `DENY_AMOUNT_EXCEEDS_LIMIT` and stops before value release. Setup and resume commands are in [`docs/mcp/reference-buyer.md`](docs/mcp/reference-buyer.md).

## Operator Control Plane

Milestone 9 adds explicit PostgreSQL-backed `operator` and `admin` assignments, recent-passkey gates, immutable operator decisions, deterministic work and alert projections, incidents, refund dispatch intents, worker health, operational metrics, and recovery runbooks. `/operator` uses these server-authoritative APIs; normal buyers receive `403`, and frontend route hiding is not authority.

Bootstrap a local operator explicitly from `apps/api` with `uv run python -m app.scripts.grant_operator acct_… --confirm`. There is no public role-grant endpoint. Manual compensation approval always uses the captured server-derived amount. Operators can invoke bounded domain reconciliation, but cannot force paid/refunded/fulfilled state or directly clear quarantine. Runbooks and forbidden actions are in [`docs/runbooks`](docs/runbooks/).

The dashboard's **Reauthenticate with Passkey** control rotates the current
server-side session only after a fresh passkey proof for the same account. This
refreshes the bounded high-risk action window without requiring an operator to
sign out or weakening Origin and CSRF enforcement.

## Domain Model

- A **merchant** has a stable opaque ID, unique URL-safe slug, public profile, lifecycle status, and audit timestamps.
- A **service** belongs to one merchant and records its type, purchase model, integer minor-unit price, machine-readable input/output schemas, fulfillment expectations, lifecycle status, and audit timestamps.
- A **quote** binds normalized service input to an immutable snapshot of the merchant-authoritative price, currency, purchase type, and fulfillment terms for a configured lifetime.
- An **account** is the canonical buyer-security subject. It binds one active buyer to its registered authentication credentials and server-side sessions; it is not a claim of legal identity.
- A **buyer policy** records immutable, time-limited per-quote constraints such as maximum amount and allowed currencies, merchants, services, service types, and purchase types.
- A **policy evaluation** is immutable evidence that a specific policy hash was compared with a specific quote hash at a recorded time and produced an `allow` or `deny` decision with ordered reason-coded checks.
- An **approval identity** belongs immutably one-to-one to an account and binds its private WebAuthn user handle to one or more registered passkey credentials.
- A **purchase authorization** is immutable, short-lived evidence that a registered passkey confirmed one exact server-derived review. It is not a payment, order, reservation, or record of money spent.
- A **payment transaction** is the stateful, audit-backed consumption of one authorization into one Razorpay Order. Its immutable RFC 8785 binding carries the approved terms forward after the short authorization expires.
- A **payment attempt** is one safe normalized Razorpay `pay_…` observation. Failed attempts remain evidence and do not prevent another attempt on the same Order from being captured.
- A **commerce outbox event** is the durable, deduplicated handoff from the first paid transition to asynchronous entitlement issuance.
- An **entitlement** is immutable, short-lived, exact-resource evidence derived only from freshly reverified paid state; it permits one logical merchant execution.
- A **fulfillment execution** is the stateful, append-only-audited attempt to deliver that resource. Its terminal success stores a bounded integrity-hashed result for safe replay.
- A **compensation case** is the immutable-binding decision record produced from a terminal paid fulfillment failure and the quote's snapshotted refund policy.
- A **payment refund** is a separate monotonic aggregate for one server-derived partial or full Razorpay refund; it never rewrites the payment transaction's captured history.
- Merchant slugs are globally unique. Service slugs are unique within their merchant. Public removal is lifecycle-based; there are no hard-delete endpoints.

## Local Development

Prerequisites: Docker Desktop with Docker Compose, Python 3.13, [`uv`](https://docs.astral.sh/uv/), Node.js 20.9 or newer, and npm.

1. Copy `.env.example` to the repository-root `.env` and replace the local-only `change-me` password in both PostgreSQL values. Payment and refund execution are safely disabled by default; keep Razorpay placeholders empty unless following the Test Mode setup below, and keep `REFUNDS_ENABLED=false` unless deliberately exercising compensation. This root file is the single dotenv source for Docker Compose and FastAPI.
2. Start PostgreSQL and Redis from the repository root:

   ```powershell
   docker compose up -d --wait
   ```

3. Prepare the database and start FastAPI from a second terminal:

   ```powershell
   Set-Location apps/api
   uv sync --frozen --dev
   uv run alembic upgrade head
   uv run python -m app.scripts.seed_dev
   uv run fastapi dev app/main.py
   ```

   FastAPI resolves `.env` from the repository path, not the current working directory. Restart it after changing `.env` so the validated startup settings are rebound.

4. In a third terminal, start Next.js with only the public API origin in its process environment:

   ```powershell
   Set-Location apps/web
   npm install
   $env:NEXT_PUBLIC_API_URL = "http://localhost:8000"
   npm run dev
   ```

The API liveness endpoint is `http://localhost:8000/health`. The readiness endpoint is `http://localhost:8000/health/ready` and reports PostgreSQL and Redis connectivity separately. Interactive API documentation is available at `http://localhost:8000/docs`, the public catalog at `http://localhost:8000/api/v1/catalog`, and the frontend at `http://localhost:3000`.

## Development Seed

`uv run python -m app.scripts.seed_dev` idempotently creates the synthetic OrbitIntel merchant and its three demonstration services. It uses no live satellite or payment data and can be run repeatedly without creating duplicates.

Merchant and service POST/PATCH routes require an active `admin` assignment, recent passkey authentication, an allowed Origin, and a valid CSRF token. Routine operators and buyer/MCP sessions cannot change catalog terms. Public catalog/merchant/service reads remain available. Trusted local seed scripts provision the reference merchant; self-service merchant ownership and onboarding are not implemented.

## Authenticated Buyer Boundary

A MeterGate Account proves control of registered authentication credentials. It does not establish legal identity or KYC. There are no passwords, email-verification claims, bearer tokens, or browser `localStorage` credentials.

First signup is a two-step WebAuthn ceremony. `POST /api/v1/auth/signup/options` stores only short-lived, versioned signup state in a dedicated Redis namespace. The browser creates a passkey, then `POST /api/v1/auth/signup/verify` atomically consumes that challenge, requires WebAuthn user verification, and transactionally creates the active `acct_…` Account, its `aid_…` ApprovalIdentity, and its first `pkc_…` credential. A failed ceremony creates no active database account.

Login uses discoverable credentials. `POST /api/v1/auth/login/options` does not accept an account, identity, or subject and omits `allowCredentials`; after the browser assertion, `POST /api/v1/auth/login/verify` resolves the credential server-side, locks Account → ApprovalIdentity → PasskeyCredential, validates RP, Origin, challenge, signature, user handle, user verification, account state, and authenticator-counter semantics, then issues a session.

The session ID is a high-entropy `ses_…` secret stored only in Redis and an HttpOnly cookie. Redis binds it to the account, approval identity, authenticating credential, account session version, authentication time, CSRF token, and finite expiry. Every protected request reloads the current Account and rejects expired, revoked, malformed, version-stale, pending, or disabled state. `GET /api/v1/auth/session` returns only safe account/session metadata and a synchronizer CSRF token; it never returns the session ID. `POST /api/v1/auth/logout` atomically revokes the Redis session and expires the cookie.

Cookie behavior is configured with `AUTH_SESSION_TTL_SECONDS`, `AUTH_REAUTH_MAX_AGE_SECONDS`, `AUTH_COOKIE_NAME`, `AUTH_COOKIE_SECURE`, `AUTH_COOKIE_SAMESITE`, `AUTH_COOKIE_DOMAIN`, and `AUTH_COOKIE_PATH`. Local HTTP works for `localhost`; deployments must enable secure settings appropriate to their HTTPS origin. Configuration rejects `SameSite=None` with `Secure=false` and enforces `__Host-` cookie requirements.

All authenticated POST routes require exactly one Origin from `WEBAUTHN_EXPECTED_ORIGINS` and the Redis-bound synchronizer token in `X-CSRF-Token`. Safe authenticated GETs require the cookie but not the CSRF header. Signup and login ceremonies also require an allowed Origin. Additional passkey enrollment requires an active owning session and authentication no older than `AUTH_REAUTH_MAX_AGE_SECONDS`. Credential deletion is intentionally deferred because MeterGate has no recovery flow and must not strand an account by removing its last usable credential.

Public discovery is separate from buyer authority: catalog, service discovery, quote creation, and quote retrieval remain public. Policy creation, policy evaluation, approval-identity access, additional passkey enrollment, approval challenges, and purchase-authorization retrieval require the owning account. Opaque IDs are locators, never authorization proof.

The Milestone 6 migration creates one deterministic Account for every existing development ApprovalIdentity, adds the non-null unique `account_id` relationship, and leaves historical policy and authorization subjects and hashes untouched. Legacy identities retain their historical `subject_ref`; ownership recognizes that preserved subject for existing evidence only. All newly created policies and identities use the Account ID as their server-derived canonical subject.

## Catalog API

`GET /api/v1/catalog` returns only active merchants and active services in deterministic order. Prices use integer minor units with an explicit currency, and each service includes its purchase type, JSON input/output schemas, and fulfillment characteristics. `GET /api/v1/catalog/services/{service_id}` returns one publicly discoverable service; inactive or unknown records return `404`.

## Quote Engine

`POST /api/v1/quotes` accepts only a `service_id` and service `input`. MeterGate validates that input with bounded, self-contained Draft 2020-12 JSON Schema rules, loads the active merchant and service, and derives the amount, currency, purchase type, expiry, and fulfillment terms on the server. External schema references and regex keywords are rejected so quote validation cannot perform network resolution or unbounded regular-expression work. The client cannot choose the payable amount or other commercial terms.

Each successful request creates a fresh immutable quote. Its PostgreSQL snapshot remains meaningful after later service edits, and its deterministic SHA-256 fingerprint binds the canonical input and commercial terms. Quote state is derived from the configured `QUOTE_TTL_SECONDS`; expiry never mutates the row. `GET /api/v1/quotes/{quote_id}` returns the original snapshot-backed quote. These endpoints issue offers only—they do not authorize spending, reserve funds, or create a payment.

## Buyer Policy Engine

`POST /api/v1/policies` creates an immutable, time-limited set of buyer constraints, and `GET /api/v1/policies/{policy_id}` retrieves its integrity-verified snapshot for the owning account. The merchant quote remains authoritative for price and commercial terms; the buyer policy only limits which quote terms are acceptable. The request cannot submit `subject_ref`; the API derives it from the authenticated Account ID.

Allowlist semantics are explicit: `null` is unconstrained, a non-empty array of at most 100 unique values permits only its listed values, and an empty or duplicate-containing array is invalid. Accepted set-like arrays are sorted before persistence and RFC 8785 hashing, so input order has no policy meaning. `POLICY_MAX_TTL_SECONDS` caps client-requested policy lifetimes. Policy hashes are deterministic integrity fingerprints, not signatures.

`POST /api/v1/policy-evaluations` accepts only a policy ID and quote ID and requires ownership of that policy; the quote remains public commerce evidence. MeterGate reloads both immutable records, rechecks their hashes, and evaluates freshness, the per-quote maximum amount, currency, merchant, service, snapshotted service type, and purchase type in a fixed order. It persists the copied policy and quote hashes, the `allow` or `deny` decision, and all safe independent reason-coded checks. A well-formed hash mismatch is recorded as a fail-closed denial with `INTEGRITY_*` reason codes and no later checks; structurally unreadable integrity material returns a sanitized server error and creates no evaluation. `GET /api/v1/policy-evaluations/{evaluation_id}` requires the same ownership, replays the versioned engine against the immutable parent records at the stored evaluation time, and rejects inconsistent evidence without consulting mutable merchant or service rows.

For example, a ₹5 quote under a policy capped at ₹10 is allowed; a ₹15 quote under that same cap is denied with `DENY_AMOUNT_EXCEEDS_LIMIT`. The maximum is per candidate quote, not cumulative spend. An `allow` result means only that the quote satisfied the policy at evaluation time—it does not approve a purchase, reserve funds, authorize payment, or record money as spent.

## Trusted Approval

Policy `allow` **does not equal** purchase authorization. MeterGate issues an authorization only after the authenticated account's active approval identity completes explicit passkey/WebAuthn user verification. There is no plain boolean approval endpoint and no fallback that an AI agent can perform.

The account-bound flow is:

1. Sign up with the first passkey or sign in with an existing discoverable passkey; signup creates the account's approval identity automatically.
2. Create a buyer policy whose subject is derived server-side from the current Account ID, request a public quote, and evaluate the owned policy against it.
3. After an integrity-verified, still-fresh `allow` evaluation, request an approval challenge using only the evaluation ID. The API selects the authenticated account's identity and derives the merchant, service, integer minor-unit amount, currency, purchase type, policy checks, hashes, and expiries from PostgreSQL.
4. Review the exact server payload bound by its RFC 8785 `review_hash`, then choose **Approve with Passkey**. The browser requires WebAuthn user verification, and the API atomically consumes the challenge, replays all evidence, rechecks quote/policy freshness, recomputes the review, verifies the assertion, and transactionally advances the authenticator counter while inserting `aut_…` evidence.
5. Retrieve safe audit fields with `GET /api/v1/authorizations/{authorization_id}`. Authorization state is derived from its configured short expiry; no row is mutated when time passes.

Redis contains only ephemeral ceremony and session state. PostgreSQL remains authoritative for accounts, identities, passkey credentials, and authorizations. API responses never expose credential raw-ID bytes, public-key bytes, authenticator counters, raw authenticator signatures, session IDs, or Redis state. The supplied localhost RP, Origin, CORS, and cookie defaults are for development only.

## Razorpay Test Mode

MeterGate uses the official Razorpay Python SDK for Orders, Payment reads, and backend-controlled idempotent refunds. It has no live-mode switch: `PAYMENTS_ENABLED=true` requires `RAZORPAY_MODE=test`, a Key ID beginning with `rzp_test_`, a Key Secret, and a dedicated webhook secret. Startup validation rejects incomplete or live-mode credentials. Secrets are server-only `SecretStr` settings; the browser receives only the public Test Mode Key ID and its exact server-created Order configuration.

Set these values in the repository-root `.env`, never in `apps/web` or a `NEXT_PUBLIC_*` variable:

```dotenv
PAYMENTS_ENABLED=true
REFUNDS_ENABLED=false
RAZORPAY_MODE=test
RAZORPAY_KEY_ID=rzp_test_replace_me
RAZORPAY_KEY_SECRET=replace_me
RAZORPAY_WEBHOOK_SECRET=replace_with_a_dedicated_test_webhook_secret
# Optional only during a deliberate secret-rotation overlap; it must differ.
RAZORPAY_PREVIOUS_WEBHOOK_SECRET=
```

Restart the API and workers after credential changes. Test Mode exercises Razorpay's real API and Checkout integration against simulated rails; it does not charge real money.

To run the explicitly side-effecting provider acceptance (it creates one genuine
₹5.00 Razorpay Test Mode Order, then fetches it directly and by receipt):

```powershell
cd apps/api
$env:RUN_RAZORPAY_TEST_MODE = "1"
uv run pytest -q -s tests/test_razorpay_test_mode_integration.py
Remove-Item Env:RUN_RAZORPAY_TEST_MODE
```

The ordinary test suite skips this check so local and CI runs never create
provider objects unexpectedly.

## Razorpay Test Refunds

`REFUNDS_ENABLED` is an independent fail-closed execution switch and defaults to `false`. Enabling it requires `PAYMENTS_ENABLED=true`, valid Razorpay Test Mode credentials, and a refund-worker lease long enough to cover the bounded exact-receipt preflight plus create-operation budget. No browser, buyer API, capability, or AI output can choose a refund amount or invoke the Razorpay refund API directly. Keep the switch off for ordinary payment and fulfillment development; set it to `true` only when deliberately running the refund worker against Test Mode.

Each provider request uses the local `rfd_…` refund ID as both Razorpay's receipt and the `X-Refund-Idempotency` value, always sends the exact server-derived amount, and requests normal-speed processing. The ordinary suite uses fakes and performs no provider refund. To run the explicitly side-effecting acceptance against a captured Test Mode Payment supplied by the operator:

```powershell
Set-Location apps/api
$env:RUN_RAZORPAY_REFUND_TEST_MODE = "1"
$env:RAZORPAY_REFUND_TEST_PAYMENT_ID = "pay_replace_with_captured_test_payment"
uv run pytest -q -s tests/test_razorpay_refund_test_mode_integration.py
Remove-Item Env:RUN_RAZORPAY_REFUND_TEST_MODE
Remove-Item Env:RAZORPAY_REFUND_TEST_PAYMENT_ID
```

The test refuses non-Test credentials, never hardcodes a payment ID, prints a warning before the side effect, and is skipped unless the explicit run flag is set; that opted-in run then requires the Payment ID. Running it creates a real Razorpay Test Mode refund against the configured payment, polls boundedly until the refund is `processed`, and fails if it remains pending or becomes failed. Ordinary automated test results do not constitute proof that this manual acceptance was performed.

The completed 26 August 2026 Test Mode failure-to-refund acceptance, including
the actual Razorpay `rfnd_…` identifier, fresh provider read, local aggregate
states, and ordered audit evidence, is preserved in
[`docs/evidence/2026-08-26-razorpay-test-mode-refund.md`](docs/evidence/2026-08-26-razorpay-test-mode-refund.md).

## Payment Boundary

`POST /api/v1/payment-transactions` accepts only an `authorization_id`. Under the authenticated Account lock, MeterGate reloads the authorization, resolves ownership through its ApprovalIdentity, recomputes authorization, policy, quote, and evaluation integrity, requires a still-active one-time authorization, and derives merchant, service, integer minor-unit amount, and currency exclusively from PostgreSQL. A unique `authorization_id` constraint makes repeated or concurrent calls return the same transaction.

## Payment Transaction

The authorization-to-transaction claim commits before any network call. The transaction ID is also the stable Razorpay receipt and fits the provider's receipt limit. A successful provider response must exactly match receipt, amount, currency, and supported status before its `order_…` ID is bound. A timeout is recorded as `order_creation_uncertain`; it is reconciled by exact receipt before any retry, never blindly duplicated. The original authorization expiry only controls the first claim. Once claimed, the immutable transaction binding—not a mutation of `PurchaseAuthorization`—carries the intent through Checkout and later webhooks.

Standard Checkout opens only after a user action and receives its order configuration from the API. Its handler sends only the `razorpay_payment_id`, `razorpay_order_id`, and `razorpay_signature` to the owning, Origin- and CSRF-protected verification route. MeterGate compares the returned order ID but computes HMAC over the order ID stored in PostgreSQL. A valid callback signature proves binding, not payment success: MeterGate fetches Razorpay's Order and Payment and reports `paid` only for exact matching captured/paid provider evidence. `authorized` is pending, and one failed attempt does not kill the Order.

Payment is not fulfillment. **VERIFIED PAYMENT CAPTURED** is durable payment history, not a claim that the merchant delivered value or that commerce is complete. Milestone 7 adds separately audited entitlement and fulfillment stages; Milestone 8 adds separate compensation and refund evidence for a qualifying terminal failure without changing the transaction from `paid`.

## Webhook Worker

Razorpay calls the public `POST /api/v1/webhooks/razorpay` route without a buyer cookie or CSRF token. MeterGate reads a bounded raw body exactly once, requires one event ID and signature, and verifies HMAC with the current dedicated webhook secret—or an explicitly configured previous secret during a bounded rotation overlap—before JSON parsing. Ordinary value-granting events retain the configurable 300-second baseline replay age. Signed refund notices may be admitted for up to Razorpay's 15-day dashboard replay horizon because they can only quarantine or close value release, never grant it. Invalid signatures, future-dated events, stale value-granting events, and refund events older than that horizon are rejected.

Before acknowledging a correlatable signed refund notice, ingress atomically closes value release and stores sticky refund evidence; an unmatched event remains available for the worker's receipt-based recovery rather than being terminally consumed. After that admission fence, the verified body is appended to `metergate:razorpay:webhooks:v1`. Local Redis runs with AOF `appendfsync=always` and `noeviction`; PostgreSQL remains the normalized audit authority. If the queue write fails after quarantine, Razorpay receives a retryable failure while value release stays closed. The queued worker strictly validates the signed refund identity, Payment and Order bindings, amount, currency, and cumulative refunded total. A validated entity status of `processed` can complete compensation; the webhook event name by itself cannot.

Run the independent consumer from `apps/api`:

```powershell
uv run python -m app.workers.razorpay_webhooks
```

The worker uses a consumer group, recovers abandoned pending entries, and acknowledges an item only after an atomic PostgreSQL commit. `x-razorpay-event-id` is unique in the append-only normalized event table, so redelivery is harmless. Raw payloads remain ephemeral in Redis; PostgreSQL retains only the body hash, event/Order/Payment/refund IDs, timestamps, safe processing outcome, attempts, and audit events. Webhooks are reconciliation triggers rather than trusted state assignments, so duplicates and out-of-order delivery cannot regress captured payment or processed refund evidence.

## Refund Webhooks

In the Razorpay Dashboard's **Test Mode**, MeterGate requires exactly these payment and refund events:

- `payment.authorized`
- `payment.captured`
- `payment.failed`
- `order.paid`
- `refund.created`
- `refund.processed`
- `refund.failed`
- `refund.speed_changed`

These are the four supported payment events and all four official Razorpay refund events used by MeterGate; do not configure or synthesize a `refund.completed` event. Every refund event must carry a refund entity bound to the same Payment, exact local refund identity, captured payment total, currency, and cumulative refunded amount. MeterGate treats its signed entity status—`pending`, `processed`, or `failed`—as normalized evidence; worker and explicit reconciliation paths additionally inspect the complete bounded payment-scoped refund collection. In particular, `refund.created` can already contain `status=processed`, while `refund.processed` delivery may be duplicated or late.

## Payment Reconciliation

The owning account can call `POST /api/v1/payment-transactions/{transaction_id}/reconcile` with its normal Origin and CSRF protections. MeterGate fetches the stored Razorpay Order and all its Payment attempts, verifies order ID, receipt, amount, currency, and each payment binding, and monotonically rebuilds local state. This repairs a lost browser callback, delayed webhook, temporary worker outage, or ambiguous Order-creation response. No client-provided provider status is accepted.

## Refund Reconciliation

A refund timeout, unavailable response, or malformed create response is ambiguous because Razorpay may have accepted the request. MeterGate records `refund_uncertain` and never blindly creates another refund. The refund worker resumes from the same durable `refund:<compensation_case_id>` handoff and searches the captured Payment's bounded refund collection for the exact stable `rfd_…` receipt or known `rfnd_…` binding. A full provider page is treated as incomplete rather than as proof of absence. Zero matches after an attempted create remain quarantined for a later retry; unreserved, duplicate, cross-payment, aggregate-total, amount, receipt, or currency conflicts enter `reconciliation_required`.

Only a validated provider refund with `status=processed` may move the `PaymentRefund` to `refunded` and close its `CompensationCase`. `pending` remains in progress, and `failed` is durable failure evidence. Provider reads, webhook deliveries, and worker retries are monotonic: late or out-of-order evidence cannot regress terminal facts or create a second provider refund. Contradictory terminal evidence opens an orthogonal reconciliation overlay and append-only audit event, projects `manual_review`, and clears only after an exact authoritative API read confirms the stored terminal fact.

### Local Test Mode webhooks with zrok

Razorpay cannot deliver webhooks to `localhost`, and its documentation recommends zrok for local testing. With the API listening on port 8000, authenticate/install zrok according to its current documentation and run:

```powershell
zrok share public localhost:8000
```

In the Razorpay Dashboard's **Test Mode**, configure the resulting HTTPS URL plus `/api/v1/webhooks/razorpay`, use the same dedicated secret as `RAZORPAY_WEBHOOK_SECRET`, and subscribe to the exact eight events listed under **Refund Webhooks**. Start PostgreSQL, Redis, the webhook worker, refund worker, API, and frontend before exercising compensation. A deployed HTTPS staging API can be used instead; do not use a tunnel hostname currently blocked by Razorpay.

### Manual Milestone 6B Windows Hello payment acceptance

Physical authenticator acceptance cannot be replaced by an automated fake. On a Windows development machine with Chrome or Edge and Windows Hello configured:

1. Run `docker compose up -d --wait` from the repository root.
2. Configure Razorpay Test Mode credentials and the Test Mode webhook as described above. In `apps/api`, run `uv sync --frozen --dev`, `uv run alembic upgrade head`, `uv run python -m app.scripts.seed_dev`, `uv run python -m app.workers.razorpay_webhooks`, and—in another terminal—`uv run fastapi dev app/main.py`.
3. In `apps/web`, set `$env:NEXT_PUBLIC_API_URL = "http://localhost:8000"`, run `npm install`, then `npm run dev`.
4. Open `http://localhost:3000` exactly (the default WebAuthn RP is `localhost`).
5. Choose **Create Account**, enter a display name, register the first passkey, and complete Windows Hello. Confirm the page shows the active `acct_…` account, its display name, and authentication method **Passkey**.
6. Sign out, confirm the buyer-authority controls show **Sign in to continue**, then choose **Sign in with Passkey** and complete Windows Hello.
7. Request the ₹5.00 INR OrbitIntel quote, create a policy capped at 1000 paise (₹10.00), and evaluate it to `allow`. Confirm the policy subject shown by the server is the signed-in Account ID; the UI must not ask for `dev-user-001` or another subject.
8. Prepare the trusted review, confirm the server-derived terms and review hash, choose **Approve with Passkey**, and complete Windows Hello again.
9. Confirm the UI shows `AUTHORIZED`, an `aut_…` ID, the exact ₹5.00 terms, an expiry and authorization hash, and **TEST MODE — NO REAL MONEY WILL BE CHARGED**.
10. Choose **Pay ₹5.00 with Razorpay**, confirm genuine Razorpay Standard Checkout opens with the same server-derived terms, and complete one successful Test Mode payment. Confirm the browser remains in verifying/pending state until the API observes capture, then shows **VERIFIED PAYMENT CAPTURED** with durable `txn_…`, `order_…`, and `pay_…` identifiers.
11. Repeat with a failed Test Mode attempt and confirm it is retained as a failed attempt without marking the transaction paid or preventing a later attempt on the same Order.
12. With `FULFILLMENT_ENABLED=false`, confirm the Dashboard deliveries or API reconciliation converge on the same `paid` result and that no entitlement, report, merchant API call, protected-resource unlock, or refund occurs.
13. Sign out and request `GET /api/v1/authorizations/<aut_id>` and `GET /api/v1/payment-transactions/<txn_id>` without the session cookie; confirm `401 AUTH_SESSION_REQUIRED`.
14. Sign back in with the same passkey and retrieve both records through the credentialed frontend flow; confirm they succeed.
15. Use automated ownership tests to confirm a second account receives `403 AUTH_RESOURCE_OWNERSHIP_MISMATCH` for the first account's policy, evaluation, identity, challenge, authorization, or payment transaction.

Windows Hello is a physical acceptance step and cannot be claimed from automated WebAuthn stubs. Record the actual browser and authenticator result when performing this checklist.

## Paid Entitlements

The first transition of a legitimate `PaymentTransaction` to `paid` inserts one `ENTITLEMENT_ISSUANCE_REQUESTED` outbox event in the same PostgreSQL commit. The browser is not responsible for this handoff. The entitlement worker claims due rows with a fenced lease and `FOR UPDATE SKIP LOCKED`, retries temporary failures with bounded backoff, and marks work processed only in the commit that creates or finds the entitlement. A unique `entitlement:<transaction_id>` outbox key and `UNIQUE(entitlements.transaction_id)` provide effective exactly-once issuance: one transaction can produce at most one entitlement.

Before releasing value, the worker reloads the immutable transaction evidence and makes fresh Razorpay server-side reads through a hard-total-deadline, bounded-concurrency adapter. It requires the exact stored Order, receipt, integer amount, currency, paid Order totals, and exactly one captured, unrefunded Payment bound to that Order. An unavailable provider, a reconciliation-required transaction, contradictory evidence, or an integrity mismatch leaves the outbox pending and creates no entitlement. The worker runs expiry finalization even when new issuance or its provider is disabled, and it does not claim new issuance work it cannot safely process. Configuration validation requires both outbox and execution leases to cover the complete provider-proof deadline plus a database margin.

An `ent_…` is immutable, RFC 8785 hash-bound evidence for one account, transaction, authorization, evaluation, policy, quote, merchant, service, normalized input, amount, currency, and provider re-verification revision. Current one-time services set `maximum_executions` to `1`, and the default entitlement lifetime is 600 seconds. The original `PurchaseAuthorization` expiry gates only the creation of a new payment transaction. Once a valid authorization has already been claimed into a durable transaction, its later expiry does not invalidate a captured payment or block entitlement issuance; paid state, transaction integrity, reconciliation state, and fresh provider verification govern issuance.

Buyer-owned access is exposed through:

- `GET /api/v1/payment-transactions/{transaction_id}/entitlement` — returns `202` while durable work is pending and includes the reason-coded transaction/fulfillment timeline.
- `GET /api/v1/entitlements/{entitlement_id}` — returns the immutable entitlement snapshot.
- `POST /api/v1/entitlements/{entitlement_id}/capability` — explicitly creates temporary bearer access after ownership, integrity, expiry, Origin, CSRF, and current local paid/reconciliation checks.

The idempotent `app.scripts.backfill_entitlement_outbox` command queues eligible paid transactions created before Milestone 7 without duplicating existing work or entitlements.

## HTTP 402 Resource Flow

The generic paid-resource entry point is:

```http
POST /api/v1/resources/{merchant_slug}/{service_slug}/execute
Content-Type: application/json

{"norad_id": 25544}
```

Without an `Authorization` header, MeterGate reads at most 256 KiB before JSON parsing, validates and normalizes the service input, and returns `402 Payment Required` with `Cache-Control: private, no-store`. The response is a purchase recipe, not a quote or proof of payment: it identifies the active merchant and service, supplies the authoritative `POST /api/v1/quotes` request, names Razorpay as the payment provider, and declares Bearer access with one maximum execution.

```json
{
  "type": "metergate_payment_required",
  "protocol": "metergate/1",
  "merchant": {"id": "mrc_...", "slug": "orbitintel", "name": "OrbitIntel"},
  "service": {"id": "svc_...", "slug": "orbital-risk-report", "name": "Orbital Risk Report"},
  "quote": {
    "endpoint": "/api/v1/quotes",
    "request": {"service_id": "svc_...", "input": {"norad_id": 25544}}
  },
  "payment_provider": "razorpay",
  "access": {"scheme": "Bearer", "maximum_executions": 1}
}
```

The client follows the existing quote → policy → passkey approval → Razorpay payment flow, polls the transaction entitlement endpoint, explicitly generates a capability, and retries the exact resource and input with `Authorization: Bearer <capability>`. A malformed credential is rejected rather than converted into another `402`; expired, tampered, wrong-resource, and wrong-input capabilities also fail closed.

## Capability Tokens

The capability is a short-lived HS256 JWT with fixed issuer `MeterGate` and audience `MeterGate protected resource gateway`. Its exact claim set binds a unique `cap_…` ID, account, entitlement, transaction, merchant, service, quote and quote hash, normalized input hash, `maximum_executions=1`, issue time, and expiry. Its expiry is the earlier of `ENTITLEMENT_TOKEN_TTL_SECONDS` and the entitlement expiry.

A capability is a bearer credential: anyone who steals it may try to use it until it expires. The mitigations are a short lifetime, fixed audience and algorithm, exact merchant/service/input bindings, server-side entitlement integrity checks, and a durable one-execution claim. It grants no payment, refund, account-management, session, CSRF, passkey, or merchant-administration authority. The signed token alone never enforces one-time use.

`ENTITLEMENT_TOKEN_SECRET` must be independent secret material of at least 32 bytes and remains server-side. The token is never written to audit metadata. The frontend keeps it only in volatile component memory, does not display it, and does not place it in `localStorage`, `sessionStorage`, URLs, analytics, or console logs. Capability responses and protected results default to `private, no-store`.

## Fulfillment

Payment captured is not fulfillment complete. A paid transaction authorizes the entitlement worker to prepare narrowly scoped access; commerce completes only when the merchant returns a valid result and the durable `FulfillmentExecution` reaches `succeeded`.

`UNIQUE(fulfillment_executions.entitlement_id)` permits one `ful_…` aggregate per entitlement. PostgreSQL row locking atomically claims or resumes `pending`, `executing`, `retryable_failure`, `succeeded`, `permanent_failure`, or `reconciliation_required` state. Concurrent exact calls see one logical execution; a request arriving while the lease is active receives `202 FULFILLMENT_ALREADY_CLAIMED`. Every merchant retry reuses the same `ful_…` idempotency key. This is effective exactly-once value delivery across MeterGate and the merchant's idempotency boundary, not a claim that a distributed network makes exactly one HTTP attempt.

The gateway verifies the immutable paid quote and entitlement bindings, canonicalizes the request against the quote's snapshotted input schema, and requires the current route merchant and service, entitlement, capability, quote, and input hash to agree. A locked local paid/anomaly gate protects both stored-result replay and claim. Every request that actually owns a dispatch lease then performs a fresh authoritative Razorpay proof; immediately before merchant I/O, one transaction locks the payment, execution, and live fulfillment configuration, rechecks the local gate and entitlement expiry, renews the generation-fenced lease, pins the configuration ID/revision, provider, endpoint, timeout, and retry bound on first dispatch, and commits `MERCHANT_REQUEST_SENT`. A disabled or missing live configuration stops dispatch, while later configuration edits cannot redirect an uncertain retry to another merchant idempotency domain.

Merchant output must be a supported JSON media type (`application/json` or `application/*+json`) and match the quote's snapshotted output content type and JSON Schema before it can cross the result boundary. The generic adapter verifies execution ID, service ID, and input hash; service-specific relationships such as OrbitIntel's NORAD identity are enforced by the paid output schema and merchant contract, not hard-coded into the generic transport. Successful JSON is size-bounded, RFC 8785 canonicalized, stored with content type, byte count, completion time, and `sha256:…` result hash. A final locked payment gate prevents a refund/anomaly observed during merchant work from releasing the result. Later catalog edits do not change the purchased contract. A later exact call returns the stored result with `replayed_result=true` and does not invoke the merchant again.

Append-only fulfillment events record entitlement and capability issuance, execution claims and starts, merchant request/response evidence, retry scheduling, success, failure, and compensation requirements. They contain stable IDs, safe hashes, reason codes, timestamps, and bounded metadata—never the capability, Razorpay secrets, session secrets, or passkey material.

## OrbitIntel Reference Merchant

`apps/orbitintel` is an independent FastAPI process, not payment or entitlement code embedded in MeterGate. MeterGate reaches only its narrow private contract:

```http
POST /internal/v1/fulfillments/{fulfillment_execution_id}
Authorization: Bearer <ORBITINTEL_SHARED_SECRET>
```

The request supplies the service identity, validated immutable input, and input hash. The shared secret is independent of the capability and Razorpay secrets. Local development binds OrbitIntel to `127.0.0.1:8100`; production must use a private network and authenticated service-to-service connection. The internal URL and credential are not published in the catalog, and OrbitIntel's paid business endpoint must not be exposed as an unauthenticated public bypass.

OrbitIntel authenticates that private request before consuming its body, rejects duplicate or invalid `Content-Length`, and streams at most `ORBITINTEL_REQUEST_MAX_BYTES` (256 KiB by default) before JSON validation. Oversized and malformed requests fail with stable `413 ORBITINTEL_REQUEST_BODY_TOO_LARGE` and `422 ORBITINTEL_REQUEST_INVALID` responses.

Every successful OrbitIntel response and stored idempotent replay echoes the fulfillment execution ID, service ID, and canonical input hash. MeterGate verifies all three before accepting the result. OrbitIntel binds the request to the paid NORAD input, and the seeded service-specific output schemas enumerate and bound the complete result—including the relevant NORAD identity—while rejecting unknown fields, so a valid HTTP response is still untrusted until it satisfies the immutable paid quote contract.

The seeded services are `satellite-status-lookup`, `orbital-risk-report`, and `detailed-orbital-analysis`; each accepts exactly a modern-range integer `norad_id`. OrbitIntel associates the `ful_…` key with the exact request binding in namespaced Redis idempotency state for the configured TTL. An identical retry returns the same persisted response bytes, a changed binding is rejected, and a concurrent duplicate cannot start another logical execution.

## CelesTrak

OrbitIntel fetches current General Perturbations JSON by NORAD catalog number from `https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=JSON`; it does not scrape HTML or fabricate missing observations. Its async client uses a descriptive User-Agent, fixed destination, no redirects, bounded response size, explicit connect/read timeouts, strict parsing, and a limited retry budget. Validated snapshots are cached briefly under namespaced Redis keys, with a token-owned distributed lock and single-flight wait to avoid a cache stampede. Failed responses are never cached as valid data.

Deterministic calculations use centrally defined WGS 84/two-body constants and label their units and approximations. The risk report describes heuristic orbital condition only, sets `operational_collision_assessment` to `false`, and explicitly states that it is not a conjunction warning or operational collision-risk product. The ordinary suite mocks CelesTrak; the live NORAD 25544 structural check is opt-in:

```powershell
Set-Location apps/orbitintel
$env:RUN_CELESTRAK_INTEGRATION = "1"
uv run pytest -q -m integration tests/test_celestrak_integration.py
Remove-Item Env:RUN_CELESTRAK_INTEGRATION
```

## Fulfillment Failure

Network/upstream failures and other explicitly retryable merchant errors enter `retryable_failure`; a later exact request resumes the same `ful_…` execution and idempotency key within the configured attempt bound. Invalid merchant responses fail closed into reconciliation-required evidence. A permanent merchant error, stale-execution retry exhaustion, or exhausted retryable failure enters `permanent_failure`, releases no result, and records `compensation_required=true` plus an append-only `COMPENSATION_REQUIRED` event. Fulfillment failure does not rewrite the valid captured payment as unpaid and never returns fake success.

`ORBITINTEL_DEV_FAULT_MODE=retryable|permanent` provides configuration-controlled acceptance testing only when `ORBITINTEL_ENVIRONMENT` is `development` or `test`; production startup rejects it. Milestone 8 consumes the permanent-failure obligation through the compensation boundary below. Subscriptions, unattended purchasing, and Live Mode remain outside the supported product.

## Compensation

A permanent failure is eligible for compensation only when the transaction is historically `paid`, one exact captured Payment backs it, no result was delivered, and the transaction, attempt, entitlement, fulfillment, and immutable quote bindings agree. The deterministic policy reads `refund_on_fulfillment_failure` from that purchased quote snapshot, never from a later mutable Service or from AI output. An eligible true flag creates one automatically approved full-refund `CompensationCase`; a false flag or contradictory/integrity evidence enters `manual_review`. Unpaid, retryable, successful, or value-delivered executions do not receive an automatic case.

The case and its append-only evidence preserve why compensation was recommended, approved, rejected, or closed. They are related to—but separate from—the immutable entitlement, terminal fulfillment evidence, and paid transaction. Repeated failure finalization resolves to the same case instead of authorizing another refund.

## Refund Policy

Refund authority is backend-only. The browser never submits an amount and there is no public refund-creation route. Current automatic compensation derives a full refund from the exact captured integer minor-unit amount. The model also permits a partial refund only when a future explicit deterministic policy derives it; every amount must be positive, currency-bound, and the sum of active, uncertain, reconciliation-required, and processed reservations must never exceed the captured amount.

Approval atomically creates a durable `refund:<compensation_case_id>` outbox handoff. The independent refund worker claims it with a fenced lease, performs no provider I/O while holding a long database transaction, and records every state change as append-only evidence. A provider API success response is not enough to close compensation: only trusted `processed` evidence is terminal success.

Compensation is a value quarantine. Once compensation is required or a refund is approved, pending, uncertain, or reconciliation-required, MeterGate denies new capabilities and merchant execution. The protected-resource path also reloads current server state, so a previously issued bearer capability cannot bypass the quarantine. MeterGate does not mutate the entitlement or erase captured payment history.

## Commerce Outcomes

Buyer-facing transaction reads derive one commerce outcome across payment, fulfillment, compensation, and refund evidence: `payment_pending`, `paid`, `fulfillment_pending`, `fulfilled`, `compensation_pending`, `manual_review`, or `refunded`. `fulfilled` requires durable merchant success. `compensation_pending` covers approved or in-progress compensation and ambiguous refund states. `refunded` requires both a provider refund normalized as `processed` and a completed compensation case. These outcomes supplement the payment state; even a compensated transaction remains historically `paid`.

## Exact Local Commands

Start from the repository root. If `.env` does not exist, create it from the committed placeholder file, replace both local database passwords, configure the three Razorpay Test Mode values, and set `PAYMENTS_ENABLED=true` and `FULFILLMENT_ENABLED=true`. Leave `REFUNDS_ENABLED=false` for ordinary development; set it to `true` only for a deliberate Test Mode compensation run:

```powershell
Copy-Item .env.example .env
[Convert]::ToHexString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
[Convert]::ToHexString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
```

Put the two different generated values in `ENTITLEMENT_TOKEN_SECRET` and `ORBITINTEL_SHARED_SECRET` respectively. Keep `.env` local and never reuse Razorpay, webhook, database, session, capability, or merchant-connection secrets.

Start PostgreSQL and Redis:

```powershell
docker compose up -d --wait
```

Prepare MeterGate, migrate, seed the OrbitIntel catalog/configuration, and idempotently backfill pre-Milestone-7 paid transactions:

```powershell
Set-Location apps/api
uv sync --frozen --dev
uv run alembic upgrade head
uv run python -m app.scripts.seed_dev
uv run python -m app.scripts.backfill_entitlement_outbox
```

Then keep each process running in its own fresh terminal from the repository root.

MeterGate API:

```powershell
Set-Location apps/api
uv run fastapi dev app/main.py
```

Razorpay webhook worker:

```powershell
Set-Location apps/api
uv run python -m app.workers.razorpay_webhooks
```

Entitlement/outbox worker:

```powershell
Set-Location apps/api
uv run python -m app.workers.entitlements
```

Refund outbox worker:

```powershell
Set-Location apps/api
uv run python -m app.workers.refunds
```

Private OrbitIntel merchant:

```powershell
Set-Location apps/orbitintel
uv sync --frozen --all-groups
uv run uvicorn app.main:app --host 127.0.0.1 --port 8100
```

Next.js frontend:

```powershell
Set-Location apps/web
npm install
$env:NEXT_PUBLIC_API_URL = "http://localhost:8000"
npm run dev
```

Razorpay cannot reach localhost directly. For real Test Mode webhook acceptance, keep the existing zrok setup above active and point the Test Mode webhook to `/api/v1/webhooks/razorpay`. The API, webhook worker, entitlement worker, refund worker, OrbitIntel, PostgreSQL, and Redis must all stay available while payment, value release, compensation, and refund reconciliation run.

Run each local quality-check block from a fresh repository-root terminal:

```powershell
Set-Location apps/api
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run alembic check
```

```powershell
Set-Location apps/orbitintel
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

```powershell
Set-Location apps/web
npm run lint
npm test -- --run
npm run build
```

```powershell
Set-Location packages/mcp
npm run lint
npm test
npm run build
```

From the repository root, also run:

```powershell
docker compose config --quiet
git diff --check
```
