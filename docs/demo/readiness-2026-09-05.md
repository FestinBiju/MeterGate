# Presentation readiness — 2026-09-05

Status: implementation, local automated checks, and a real browser-driven purchase/report/receipt/replay are verified. Connected-agent acceptance, a fresh failure/refund run, recording, and submission remain pending.

## Implemented

- Merchant and service creation/update require an active administrator role, recent passkey authentication, allowed Origin, and CSRF verification. Buyers and routine operators cannot change catalog terms; public discovery is preserved.
- Homepage prioritizes three guided scenarios with copyable MCP prompts and clear human handoff. Public evidence replaces unavailable metric tiles.
- `/evidence` displays the recorded policy dataset and scopes the historical Razorpay refund separately. It distinguishes external AI judgment from the reference keyword planner.
- Purchased OrbitIntel results have readable summaries for status, risk, and detailed analysis, with units, source/freshness, limitations, and expandable raw JSON.
- Purchase receipts download allowlisted JSON from fresh authenticated transaction/authorization/evaluation reads. Pending and refunded transactions do not acquire a delivered-result claim. Result evidence is bound to the transaction. Credentials and raw result payloads are excluded.
- Policy generation also writes the identical public web snapshot inside the web Docker build context.
- Sanitized transaction export now includes the provider refund ID required by the recording checklist.
- A read-only preflight command checks infrastructure, catalog/402 behavior, rejected anonymous administration, and worker heartbeats.
- Presentation script, judge Q&A, merchant integration guide, observed-run template, and submission checklist are available.

## Verification performed

| Check | Result and scope |
| --- | --- |
| Backend lint/format | Passed before the full suite; the new preflight script was separately linted/formatted. |
| Backend tests, including isolated PostgreSQL integration | 795 passed, 2 skipped; live Razorpay test files excluded. |
| Catalog authorization / contract / operator focused tests | 36 passed, including anonymous/routine-operator/buyer rejection, stale approval, Origin/CSRF checks, admin routing and malformed input. |
| Refund and entitlement database suites after fixture provisioning update | 40 passed. Only fixture catalog provisioning receives test administration; buyer/payment checks remain active. |
| Database migration | Current head `20260827_0013`; Alembic check reported no missing upgrade operations. |
| Frontend | 14 checks passed, typecheck passed, production build passed with `NEXT_PUBLIC_API_URL=http://localhost:8000`. Changed frontend files pass ESLint. |
| Merchant baseline | 70 tests passed in the review; merchant implementation unchanged. |
| Live CelesTrak structural integration | 1 passed in this execution. |
| MCP baseline | 16 tests, typecheck, and build passed in the review; MCP implementation unchanged by this execution. |
| Secret scan | Tracked content/history passed; 20 untracked deliverable files had zero credential candidates when checked. |
| Compose and whitespace | Compose validation and `git diff --check` passed. |
| Live local preflight | All ten selected checks passed at 2026-09-05T04:17:39Z. |
| Browser homepage | Real catalog loaded (₹2/₹5/₹9), infrastructure ready, guided navigation and copy feedback verified. |
| Browser evidence | All 60 recorded scenario rows accessible, scope text visible. |
| Responsive layout | Homepage and actual report component checked at desktop and narrow mobile viewport; no horizontal document overflow observed. |
| Report visual QA scope | Actual React component rendered with an explicitly labelled synthetic fixture outside the production app. This is layout evidence, not live paid delivery. |
| Live browser purchase | Human passkey approval and Razorpay Test Mode capture completed. Transaction `txn_01M1QWZ4Z460JDFZCWSM0T6KYE`, ₹5 Orbital Risk Report, NORAD 25544. Paid at 04:25:12Z; fulfillment succeeded at 04:25:24Z on 2026-09-05. |
| Live report and receipt | Readable ISS report observed in the actual purchase screen. Downloaded JSON inspected: amount, approval, paid state, and delivered result hash match the transaction; no credentials or raw result payload. |
| Live browser replay | UI returned `stored replay` with the same result hash. Read-only database check afterward found one execution with `attempt_count=1`. |
| Live browser denial | ₹9 Detailed Orbital Analysis rejected under ₹5 at 04:33:02Z with `DENY_AMOUNT_EXCEEDS_LIMIT`. Zero linked authorizations and payment transactions. Evidence: `docs/evaluation/denial-2026-09-05.json`. |
| Live evidence limitation | This was browser-driven with a ₹5 policy maximum, not the planned AI-selection run under a ₹6 maximum. The connected MCP session returned `MCP_SESSION_EXPIRED`. No webhook evidence row was linked to this transaction when checked. |

## Runtime and remaining human acceptance

Local web: `http://localhost:3000`. API: `http://localhost:8000`. Private merchant: loopback port 8100. PostgreSQL uses the configured port 5433; Redis uses port 6379. API, merchant, webhook worker, entitlement worker, and refund worker were started during this execution. Preflight observed fresh worker heartbeats.

Razorpay Test Mode payments and fulfillment are enabled. Refund dispatch is currently disabled in the ordinary process configuration. A running refund worker heartbeat does not prove refund dispatch is enabled. Enable the deliberate failure-run configuration only when preparing that scenario and verify the worker's effective configuration.

Still required:

1. Renew the MCP connection privately and record actual connected-agent selection and policy denial.
2. Operator-prepared permanent failure and provider-confirmed processed refund; live webhook delivery acceptance.
3. Complete the remaining rows in `docs/evaluation/agent-runs.md` from observed runs. Current browser success is exported in `docs/evaluation/success-2026-09-05.json`.
4. Final narration/video recording, public repository/video URL verification, and actual submission confirmation.

The separate historical refund remains the 2026-08-26 run. The 18.27-second interval measures refund request start to completion, not total purchase recovery time. Pending connected-agent runs have not been counted as successes.

## Commands

### User-requested MCP renewal correction

Implemented after the initial acceptance: an owner can renew an existing connection through a passkey-bound browser action while retaining the same token and client registration. The UI offers 15 minutes, 30 minutes, or one hour, and MCP expiry errors link to renewal. Revoked connections cannot be renewed; expired connections can also be permanently revoked. Scopes stay unchanged. Proof consumption and the renewal audit commit with the new grant.

Reverification: full backend suite **804 passed, 4 skipped** (including the two opt-in Razorpay acceptance tests); backend lint passed. Frontend **14 passed**, typecheck, changed-component lint, and production build passed. The renewal integration test uses an isolated PostgreSQL schema and explicitly constructed test proof evidence; it does not impersonate a human on the live app. Live UI duration choices and guidance were inspected; the real MCP adapter returned the updated expiry link.

Live renewal still needs the owner's passkey. The configured connection belongs to account “Festin”; the browser account “fest” is distinct and cannot renew it. Sign into the owning account to keep the existing key. Alternatively, pairing this different buyer requires one initial new configuration, after which the connection can be renewed normally.

### Local commands

From `apps/api`:

```powershell
uv run python -m app.scripts.presentation_preflight
```

From `apps/web`, before a production build:

```powershell
$env:NEXT_PUBLIC_API_URL = "http://localhost:8000"
npm run build
npm run start
```

Use the existing running processes during the rehearsal; do not launch duplicates on their ports. Use [presentation.md](presentation.md) for screen order and final submission checks.
