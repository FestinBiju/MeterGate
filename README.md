# MeterGate

**A Razorpay-native agent storefront for paid APIs and digital services.**

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

## Current Milestone

Milestone 6B adds a real Razorpay Test Mode payment gate after the passkey-first buyer-account, policy, approval, and authorization chain. MeterGate consumes one active `aut_…` into at most one durable `txn_…`, creates the Razorpay Order server-side, launches genuine Standard Checkout, verifies callback and webhook signatures, and requires captured server-side payment evidence before reporting `paid`. Test Mode does not move real money. Entitlements, fulfillment, and refunds remain intentionally out of scope.

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
- Merchant slugs are globally unique. Service slugs are unique within their merchant. Public removal is lifecycle-based; there are no hard-delete endpoints.

## Local Development

Prerequisites: Docker Desktop with Docker Compose, Python 3.13, [`uv`](https://docs.astral.sh/uv/), Node.js 20.9 or newer, and npm.

1. Copy `.env.example` to the repository-root `.env` and replace the local-only `change-me` password in both PostgreSQL values. Payment support is safely disabled by default; keep its Razorpay placeholders empty unless following the Test Mode setup below. This root file is the single dotenv source for Docker Compose and FastAPI.
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

Merchant and service management routes remain local development/admin surfaces. Milestone 6A does not add merchant authentication and does not bind buyer accounts to merchant administration.

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

MeterGate uses the official Razorpay Python SDK for Orders and Payment reads. It has no live-mode switch: `PAYMENTS_ENABLED=true` requires `RAZORPAY_MODE=test`, a Key ID beginning with `rzp_test_`, a Key Secret, and a dedicated webhook secret. Startup validation rejects incomplete or live-mode credentials. Secrets are server-only `SecretStr` settings; the browser receives only the public Test Mode Key ID and its exact server-created Order configuration.

Set these values in the repository-root `.env`, never in `apps/web` or a `NEXT_PUBLIC_*` variable:

```dotenv
PAYMENTS_ENABLED=true
RAZORPAY_MODE=test
RAZORPAY_KEY_ID=rzp_test_replace_me
RAZORPAY_KEY_SECRET=replace_me
RAZORPAY_WEBHOOK_SECRET=replace_with_a_dedicated_test_webhook_secret
```

Restart the API and worker after credential changes. Test Mode exercises Razorpay's real API and Checkout integration against simulated rails; it does not charge real money.

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

## Payment Boundary

`POST /api/v1/payment-transactions` accepts only an `authorization_id`. Under the authenticated Account lock, MeterGate reloads the authorization, resolves ownership through its ApprovalIdentity, recomputes authorization, policy, quote, and evaluation integrity, requires a still-active one-time authorization, and derives merchant, service, integer minor-unit amount, and currency exclusively from PostgreSQL. A unique `authorization_id` constraint makes repeated or concurrent calls return the same transaction.

## Payment Transaction

The authorization-to-transaction claim commits before any network call. The transaction ID is also the stable Razorpay receipt and fits the provider's receipt limit. A successful provider response must exactly match receipt, amount, currency, and supported status before its `order_…` ID is bound. A timeout is recorded as `order_creation_uncertain`; it is reconciled by exact receipt before any retry, never blindly duplicated. The original authorization expiry only controls the first claim. Once claimed, the immutable transaction binding—not a mutation of `PurchaseAuthorization`—carries the intent through Checkout and later webhooks.

Standard Checkout opens only after a user action and receives its order configuration from the API. Its handler sends only the `razorpay_payment_id`, `razorpay_order_id`, and `razorpay_signature` to the owning, Origin- and CSRF-protected verification route. MeterGate compares the returned order ID but computes HMAC over the order ID stored in PostgreSQL. A valid callback signature proves binding, not payment success: MeterGate fetches Razorpay's Order and Payment and reports `paid` only for exact matching captured/paid provider evidence. `authorized` is pending, and one failed attempt does not kill the Order.

Payment is not fulfillment. Milestone 6B stops at **VERIFIED PAYMENT CAPTURED** and does not issue an entitlement, call a paid merchant API, unlock a resource, generate the report, or initiate a refund.

## Webhook Worker

Razorpay calls the public `POST /api/v1/webhooks/razorpay` route without a buyer cookie or CSRF token. MeterGate reads a bounded raw body exactly once, requires one event ID and signature, verifies HMAC with the dedicated webhook secret before JSON parsing, enforces the configurable 300-second baseline replay age, and durably appends the verified body to the `metergate:razorpay:webhooks:v1` Redis Stream before returning `200`. Invalid or stale signatures never mutate payment state.

Run the independent consumer from `apps/api`:

```powershell
uv run python -m app.workers.razorpay_webhooks
```

The worker uses a consumer group, recovers abandoned pending entries, and acknowledges an item only after an atomic PostgreSQL commit. `x-razorpay-event-id` is unique in the append-only normalized event table, so redelivery is harmless. Raw payloads remain ephemeral in Redis; PostgreSQL retains only the body hash, event/Order/Payment IDs, timestamps, safe processing outcome, attempts, and transaction audit events. Webhooks are reconciliation triggers rather than trusted state assignments, so duplicates and out-of-order `payment.authorized`, `payment.captured`, `payment.failed`, and `order.paid` events cannot regress captured success.

## Payment Reconciliation

The owning account can call `POST /api/v1/payment-transactions/{transaction_id}/reconcile` with its normal Origin and CSRF protections. MeterGate fetches the stored Razorpay Order and all its Payment attempts, verifies order ID, receipt, amount, currency, and each payment binding, and monotonically rebuilds local state. This repairs a lost browser callback, delayed webhook, temporary worker outage, or ambiguous Order-creation response. No client-provided provider status is accepted.

### Local Test Mode webhooks with zrok

Razorpay cannot deliver webhooks to `localhost`, and its documentation recommends zrok for local testing. With the API listening on port 8000, authenticate/install zrok according to its current documentation and run:

```powershell
zrok share public localhost:8000
```

In the Razorpay Dashboard's **Test Mode**, configure the resulting HTTPS URL plus `/api/v1/webhooks/razorpay`, use the same dedicated secret as `RAZORPAY_WEBHOOK_SECRET`, and subscribe to `payment.authorized`, `payment.captured`, `payment.failed`, and `order.paid`. Start PostgreSQL, Redis, the webhook worker, API, and frontend before the payment. A deployed HTTPS staging API can be used instead; do not use a tunnel hostname currently blocked by Razorpay.

### Manual Windows Hello acceptance

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
12. Confirm the Dashboard deliveries or API reconciliation converge on the same `paid` result and that no entitlement, report, merchant API call, protected-resource unlock, or refund occurs.
13. Sign out and request `GET /api/v1/authorizations/<aut_id>` and `GET /api/v1/payment-transactions/<txn_id>` without the session cookie; confirm `401 AUTH_SESSION_REQUIRED`.
14. Sign back in with the same passkey and retrieve both records through the credentialed frontend flow; confirm they succeed.
15. Use automated ownership tests to confirm a second account receives `403 AUTH_RESOURCE_OWNERSHIP_MISMATCH` for the first account's policy, evaluation, identity, challenge, authorization, or payment transaction.

Windows Hello is a physical acceptance step and cannot be claimed from automated WebAuthn stubs. Record the actual browser and authenticator result when performing this checklist.
