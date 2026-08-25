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

Milestone 5 adds trusted human approval to the immutable quote and deterministic policy foundation. A policy `allow` result may now be bound to a real passkey/WebAuthn ceremony and a short-lived, immutable purchase authorization. Payment creation, Razorpay, authorization consumption, entitlements, and fulfillment execution remain intentionally out of scope.

## Domain Model

- A **merchant** has a stable opaque ID, unique URL-safe slug, public profile, lifecycle status, and audit timestamps.
- A **service** belongs to one merchant and records its type, purchase model, integer minor-unit price, machine-readable input/output schemas, fulfillment expectations, lifecycle status, and audit timestamps.
- A **quote** binds normalized service input to an immutable snapshot of the merchant-authoritative price, currency, purchase type, and fulfillment terms for a configured lifetime.
- A **buyer policy** records immutable, time-limited per-quote constraints such as maximum amount and allowed currencies, merchants, services, service types, and purchase types.
- A **policy evaluation** is immutable evidence that a specific policy hash was compared with a specific quote hash at a recorded time and produced an `allow` or `deny` decision with ordered reason-coded checks.
- An **approval identity** binds one application subject reference to a private WebAuthn user handle and one or more registered passkey credentials. It proves control of a registered credential, not a person's legal identity.
- A **purchase authorization** is immutable, short-lived evidence that a registered passkey confirmed one exact server-derived review. It is not a payment, order, reservation, or record of money spent.
- Merchant slugs are globally unique. Service slugs are unique within their merchant. Public removal is lifecycle-based; there are no hard-delete endpoints.

## Local Development

Prerequisites: Docker Desktop with Docker Compose, Python 3.13, [`uv`](https://docs.astral.sh/uv/), Node.js 20.9 or newer, and npm.

1. Copy `.env.example` to the repository-root `.env`, replace the local-only `change-me` password in both PostgreSQL values, and keep the Razorpay placeholders empty. This root file is the single dotenv source for Docker Compose and FastAPI.
2. Start PostgreSQL and Redis from the repository root:

   ```powershell
   docker compose up -d --wait
   ```

3. Prepare the database and start FastAPI from a second terminal:

   ```powershell
   Set-Location apps/api
   uv sync --dev
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

## Catalog API

`GET /api/v1/catalog` returns only active merchants and active services in deterministic order. Prices use integer minor units with an explicit currency, and each service includes its purchase type, JSON input/output schemas, and fulfillment characteristics. `GET /api/v1/catalog/services/{service_id}` returns one publicly discoverable service; inactive or unknown records return `404`.

## Quote Engine

`POST /api/v1/quotes` accepts only a `service_id` and service `input`. MeterGate validates that input with bounded, self-contained Draft 2020-12 JSON Schema rules, loads the active merchant and service, and derives the amount, currency, purchase type, expiry, and fulfillment terms on the server. External schema references and regex keywords are rejected so quote validation cannot perform network resolution or unbounded regular-expression work. The client cannot choose the payable amount or other commercial terms.

Each successful request creates a fresh immutable quote. Its PostgreSQL snapshot remains meaningful after later service edits, and its deterministic SHA-256 fingerprint binds the canonical input and commercial terms. Quote state is derived from the configured `QUOTE_TTL_SECONDS`; expiry never mutates the row. `GET /api/v1/quotes/{quote_id}` returns the original snapshot-backed quote. These endpoints issue offers only—they do not authorize spending, reserve funds, or create a payment.

## Buyer Policy Engine

`POST /api/v1/policies` creates an immutable, time-limited set of buyer constraints, and `GET /api/v1/policies/{policy_id}` retrieves its integrity-verified snapshot. The merchant quote remains authoritative for price and commercial terms; the buyer policy only limits which quote terms are acceptable. `subject_ref` is currently a development/application reference, not authenticated ownership.

Allowlist semantics are explicit: `null` is unconstrained, a non-empty array of at most 100 unique values permits only its listed values, and an empty or duplicate-containing array is invalid. Accepted set-like arrays are sorted before persistence and RFC 8785 hashing, so input order has no policy meaning. `POLICY_MAX_TTL_SECONDS` caps client-requested policy lifetimes. Policy hashes are deterministic integrity fingerprints, not signatures.

`POST /api/v1/policy-evaluations` accepts only a policy ID and quote ID. MeterGate reloads both immutable records, rechecks their hashes, and evaluates freshness, the per-quote maximum amount, currency, merchant, service, snapshotted service type, and purchase type in a fixed order. It persists the copied policy and quote hashes, the `allow` or `deny` decision, and all safe independent reason-coded checks. A well-formed hash mismatch is recorded as a fail-closed denial with `INTEGRITY_*` reason codes and no later checks; structurally unreadable integrity material returns a sanitized server error and creates no evaluation. `GET /api/v1/policy-evaluations/{evaluation_id}` replays the versioned engine against the immutable parent records at the stored evaluation time and rejects inconsistent evidence without consulting mutable merchant or service rows.

For example, a ₹5 quote under a policy capped at ₹10 is allowed; a ₹15 quote under that same cap is denied with `DENY_AMOUNT_EXCEEDS_LIMIT`. The maximum is per candidate quote, not cumulative spend. An `allow` result means only that the quote satisfied the policy at evaluation time—it does not approve a purchase, reserve funds, authorize payment, or record money as spent.

## Trusted Approval

Policy `allow` **does not equal** purchase authorization. MeterGate issues an authorization only after an active approval identity whose `subject_ref` matches the policy completes explicit passkey/WebAuthn user verification. There is no plain boolean approval endpoint and no fallback that an AI agent can perform.

The development flow is:

1. Create or load an approval identity for the same subject used by the buyer policy.
2. Request registration options, complete `navigator.credentials.create()` in the browser, and let the API verify and persist the credential public key. Registration challenges are short-lived and atomically single-use in Redis.
3. After an integrity-verified, still-fresh `allow` evaluation, request an approval challenge using only the evaluation and identity IDs. The API derives the merchant, service, integer minor-unit amount, currency, purchase type, policy checks, hashes, and expiries from PostgreSQL.
4. Review the exact server payload bound by its RFC 8785 `review_hash`, then choose **Approve with Passkey**. The browser requires WebAuthn user verification, and the API atomically consumes the challenge, replays all evidence, rechecks quote/policy freshness, recomputes the review, verifies the assertion, and transactionally advances the authenticator counter while inserting `aut_…` evidence.
5. Retrieve safe audit fields with `GET /api/v1/authorizations/{authorization_id}`. Authorization state is derived from its configured short expiry; no row is mutated when time passes.

Redis contains only ephemeral ceremony state. PostgreSQL remains authoritative for identities, passkey credentials, and authorizations. API responses never expose public-key bytes, raw authenticator signatures, raw challenges, or Redis state. `WEBAUTHN_RP_ID`, `WEBAUTHN_RP_NAME`, `WEBAUTHN_EXPECTED_ORIGINS`, `WEBAUTHN_CHALLENGE_TTL_SECONDS`, and `AUTHORIZATION_TTL_SECONDS` are environment-driven; the supplied localhost defaults are for development only.

Milestone 5 deliberately has no authenticated user-account bootstrap, KYC, recovery, or passkey-management authorization. The identity and passkey-enrollment routes are development setup surfaces: trust begins at enrollment, and knowledge of an `aid_…` is not an ownership proof. A production account layer must protect first enrollment and require an existing trusted factor to add or recover credentials. This milestone proves only control of a credential that MeterGate has registered for the subject.

No payment is created or executed in this milestone. A future payment gate must separately validate and consume an active authorization.

### Manual Windows Hello acceptance

Physical authenticator acceptance cannot be replaced by an automated fake. On a Windows development machine with Chrome or Edge and Windows Hello configured:

1. Run `docker compose up -d --wait` from the repository root.
2. In `apps/api`, run `uv sync --frozen --dev`, `uv run alembic upgrade head`, `uv run python -m app.scripts.seed_dev`, and `uv run fastapi dev app/main.py`.
3. In `apps/web`, set `$env:NEXT_PUBLIC_API_URL = "http://localhost:8000"`, run `npm install`, then `npm run dev`.
4. Open `http://localhost:3000` exactly (the default WebAuthn RP is `localhost`).
5. Request the ₹5.00 INR OrbitIntel quote, create a `dev-user-001` policy capped at 1000 paise (₹10.00), and evaluate it to `allow`.
6. Create a `dev-user-001` approval identity, choose **Register Passkey**, and complete the Windows Hello prompt.
7. Prepare the trusted review, confirm the server-derived terms and review hash, choose **Approve with Passkey**, and complete Windows Hello again.
8. Confirm the UI shows `AUTHORIZED`, an `aut_…` ID, the exact ₹5.00 terms, an expiry and authorization hash, plus “No payment has been created or executed yet.”
9. Verify the same safe artifact with `Invoke-RestMethod http://localhost:8000/api/v1/authorizations/<aut_id>` and confirm that no payment or checkout endpoint/state was created.
