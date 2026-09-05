# Five-minute judge demonstration

This runbook is the recording source of truth for Razorpay Buildathon Track 01. It uses real MeterGate state and Razorpay Test Mode. Do not replace any passkey, payment, provider verification, fulfillment, or refund step with a mock.

For the final submission, use the updated [five-minute presentation sequence](presentation.md): record a normal successful delivery and its receipt first, then the rejection and separate failure/refund scenario. The fault-mode steps below describe only the failure segment; do not start the success run with permanent fault injection enabled.

## Pitch framing

Say this near the opening:

> We optimized for making merchants safely transactable by AI buyers. Without a trust and control layer, merchants cannot safely expose paid APIs to agent traffic; MeterGate is the revenue-growth precondition.

Then make the AI boundary explicit:

> Codex interprets the fuzzy request, compares the catalog, and proposes a service. Deterministic code owns price, policy, approval, payment verification, entitlement, fulfillment, and refunds. We use AI where judgment helps, and deliberately do not use it as money authority.

Show concise decision summaries, not hidden chain-of-thought.

## Measured evidence block

These figures are backed by committed artifacts and must be presented with their scope:

| Evidence | Measured result | Scope |
| --- | ---: | --- |
| Agent-readable catalog | 3 services at ₹2 / ₹5 / ₹9 | Seeded OrbitIntel catalog |
| Deterministic policy evaluation | 60 / 60 scenarios passed | Generated batch evaluation |
| Policy bypass rate | 0.00% | 40 expected-denial scenarios |
| False-block rate | 0.00% | 20 expected-allow scenarios |
| Policy evaluation p95 | 0.117 ms | In-process evaluation artifact generated 2026-08-28 from the documented dirty revision |
| Real Test Mode refund recovery | 1 / 1 accepted | One historical physical acceptance run, not a population claim |
| Refund request start → completion | 18.27 s | Historical accepted refund `processed` by Razorpay Test Mode |

During the live run, show **Hackathon agent metrics** in `/operator` and narrate its current GMV, policy denials, handled failures, refund recoveries, and access latency exactly as displayed. Never replace an unavailable measurement with an estimate.

Sources: [generated policy results](../evaluation/results.md) and [real Razorpay Test Mode refund acceptance](../evidence/2026-08-26-razorpay-test-mode-refund.md).

## Standards-aligned, not standards-conformant

| Protocol | Honest relationship |
| --- | --- |
| UAP | MeterGate follows the Track 01 direction of user-controlled agent payments. No public UAP conformance claim is made. [Razorpay track brief](https://razorpay.com/buildathon/) |
| AP2 | Immutable quotes, bounded policies, passkey authorization, payment evidence, and receipts resemble AP2's checkout/payment mandate separation. MeterGate does not emit AP2 mandate JWTs. [AP2 specification](https://github.com/google-agentic-commerce/AP2/blob/main/docs/ap2/specification.md) |
| ACP | The catalog, quote, human handoff, and merchant-owned payment state overlap ACP's checkout lifecycle. MeterGate does not claim ACP endpoint compatibility. [ACP documentation](https://docs.stripe.com/agentic-commerce/protocol) |
| x402 | MeterGate uses a resource-first `402 Payment Required` recipe, but settles through Razorpay Test Mode and entitlement capabilities rather than x402 payment signatures and blockchain facilitators. [x402 specification](https://github.com/x402-foundation/x402/blob/main/specs/x402-specification-v1.md) |

## Preflight

Do the credential and service setup off camera.

1. In **Agent Connections**, renew the existing configured connection for the rehearsal using your passkey. Its key and MCP registration stay the same. Only create/register a new connection for first-time setup or to replace a lost/revoked key. Revoke abandoned or exposed keys; do not routinely revoke the connection you intend to reuse. Never show credentials in the recording, terminal history, chat, or screenshots.
2. Confirm `.env` contains valid Razorpay Test Mode credentials, `PAYMENTS_ENABLED=true`, and `FULFILLMENT_ENABLED=true`. Do not print secret-bearing configuration.
3. Start PostgreSQL and Redis:

   ```powershell
   Set-Location C:\Developer\MeterGate
   docker compose up -d --wait
   ```

4. Prepare the database and catalog:

   ```powershell
   Set-Location C:\Developer\MeterGate\apps\api
   uv run alembic upgrade head
   uv run python -m app.scripts.seed_dev
   uv run python -m app.scripts.backfill_entitlement_outbox
   ```

5. Start each process in a separate fresh PowerShell window. Environment overrides are process-local and disappear when the window closes.

   ```powershell
   # MeterGate API
   Set-Location C:\Developer\MeterGate\apps\api
   $env:DEMO_MODE = "true"
   $env:REFUNDS_ENABLED = "true"
   uv run fastapi dev app/main.py
   ```

   ```powershell
   # Webhook worker
   Set-Location C:\Developer\MeterGate\apps\api
   uv run python -m app.workers.razorpay_webhooks
   ```

   ```powershell
   # Entitlement worker
   Set-Location C:\Developer\MeterGate\apps\api
   uv run python -m app.workers.entitlements
   ```

   ```powershell
   # Refund worker — the only demo worker allowed to contact the refund API
   Set-Location C:\Developer\MeterGate\apps\api
   $env:REFUNDS_ENABLED = "true"
   uv run python -m app.workers.refunds
   ```

   ```powershell
   # Merchant with development-only permanent fault injection
   Set-Location C:\Developer\MeterGate\apps\orbitintel
   $env:ORBITINTEL_DEV_FAULT_MODE = "permanent"
   uv run uvicorn app.main:app --host 127.0.0.1 --port 8100
   ```

   ```powershell
   # Web application
   Set-Location C:\Developer\MeterGate\apps\web
   $env:NEXT_PUBLIC_API_URL = "http://localhost:8000"
   npm run dev
   ```

6. Keep the configured HTTPS Razorpay Test Mode webhook forwarding to `/api/v1/webhooks/razorpay`. Confirm API, workers, OrbitIntel, web, PostgreSQL, and Redis are healthy before recording.

## Exact prompts and screen order

### 0:00–0:35 — merchant revenue framing

Show the landing page and deliver the two framing statements above. Point out that the merchant exposes three paid services without giving Codex Razorpay credentials or refund authority.

### 0:35–1:35 — visible AI judgment and comparison

In Codex, use:

> Use only the registered MeterGate MCP tools. I need a useful orbital-condition report for NORAD 25544, one-time only, and I can spend at most ₹6. Normalize my intent, compare every relevant catalog option in a concise table using server-published scope and price, explain your selection briefly, then request the quote, create the bounded policy, and evaluate it. Do not reveal hidden chain-of-thought or any credential.

Expected evidence:

- structured intent contains NORAD `25544`, ₹6/`600` paise, INR, and `one_time`;
- Codex compares ₹2 status, ₹5 risk report, and ₹9 detailed analysis;
- it selects the ₹5 report because it matches the requested report depth and budget;
- the immutable quote and deterministic policy return `ALLOW` followed by `MCP_HUMAN_APPROVAL_REQUIRED`;
- Codex stops and provides the browser handoff URL.

### 1:35–2:10 — deterministic policy rejection

In a separate Codex task, use:

> Use only the registered MeterGate MCP tools. I specifically require Detailed Orbital Analysis for NORAD 25544, one-time only, with a hard ₹5 maximum. Do not substitute another service. Request its server quote, create the ₹5 policy, evaluate it, and explain the authoritative outcome briefly.

Expected evidence: the ₹9 quote is rejected with `DENY_AMOUNT_EXCEEDS_LIMIT`; no approval, payment, capability, fulfillment, or refund call follows.

### 2:10–3:10 — trusted human and real Test Mode payment

Open the first prompt's handoff. Show **Agent proposal**, **Deterministic checks**, and **Human authority**. Complete Windows Hello/passkey approval and Razorpay Test Mode Checkout. Do not cut from an unverified checkout callback to success; wait for backend-verified capture.

### 3:10–4:15 — permanent failure and automatic refund

Ask Codex to resume the first purchase, respecting server retry guidance. It should obtain the entitlement and ephemeral capability, then `execute_paid_resource` should fail with `FULFILLMENT_COMPENSATION_REQUIRED`. State plainly that payment was captured but no result crossed the boundary.

Ask Codex to read `get_purchase_status` no faster than `retry_after_seconds` until it returns `MCP_COMMERCE_REFUNDED`. Codex has no tool that can authorize or create the refund.

Open `/operator`, select the transaction, and show the **Payment → failure → refund** ladder plus the append-only timeline. Refresh selected evidence until the final step says **Refund processed** with provider status `processed`.

### 4:15–5:00 — metrics, protocols, close

Show **Hackathon agent metrics**, the measured evidence block, and the standards table. Close on the trust boundary: AI proposed; deterministic systems rejected the bad purchase and recovered the failed paid purchase.

## Evidence export and shutdown

Export only the sanitized transaction record:

```powershell
Set-Location C:\Developer\MeterGate\apps\api
uv run python -m app.scripts.export_transaction_evidence txn_… --output ../../docs/evidence/demo-transaction.json
```

Verify the export contains a `rfnd_…` provider identifier, terminal provider status `processed`, one compensation case, one refund, and no result release. Do not commit the export until it has passed the secret scan.

After recording:

1. Stop the faulted OrbitIntel process. Restart normally with `ORBITINTEL_DEV_FAULT_MODE=none` or no override.
2. Stop the API/refund worker windows so their process-local demo flags disappear.
3. Revoke the fresh Agent Session in the web UI and remove the local Codex MCP registration.
4. Preserve all payment, audit, entitlement, fulfillment, compensation, and refund evidence.

## Recording acceptance checklist

- The three service prices are read from the live catalog.
- Codex shows a concise comparison and selection rationale, not hidden reasoning.
- The ₹9/₹5 attempt ends at `DENY_AMOUNT_EXCEEDS_LIMIT` with no value release.
- The paid failure retains historical `paid`, releases no merchant result, and creates one compensation case.
- The refund reaches Razorpay Test Mode `processed`, with no agent refund tool and no duplicate refund.
- The operator ladder and timeline show backend evidence rather than timers or staged screenshots.
- All visible numbers are measured and scoped; all protocol language says aligned/inspired, not conformant.
