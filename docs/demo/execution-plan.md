# Buildathon execution plan

Status: active goal; security and presentation features implemented. Live browser purchase, readable report, receipt, and exact-result replay verified. Connected-agent, fresh refund, recording, and submission acceptance remain open.

See [the current readiness record](readiness-2026-09-05.md) for observed verification and limitations. G1 is implemented and tested. G2 has a healthy local runtime and verified payment/fulfillment; webhook acceptance remains. G3–G5 are implemented with data tests and desktop/mobile visual checks, plus an actual purchased report and inspected download. G6 documentation and browser-success evidence are prepared; live agent-run rows remain pending. G7 automated checks, browser replay, and presentation materials are ready, while connected-agent/refund acceptance, recording, and submission remain open. Detailed checklist items below remain acceptance criteria; do not interpret an unobserved human step as completed.

Prepared on 2026-09-05 following the repository-wide readiness review. Preserve the existing working-tree changes. This plan covers the three submission features proposed in the review: a readable purchased report, a downloadable purchase receipt, and guided demo scenarios. Larger future products are tracked separately below.

## Outcome

A judge can understand the merchant problem, watch an AI select an appropriate paid service, inspect deterministic spending checks, see human approval and a real Razorpay Test Mode payment, read the delivered result, download its purchase evidence, and inspect graceful recovery when fulfillment fails.

Presentation-ready means verified behavior, clear presentation, reproducible setup, and honest evidence. It does not mean an unqualified production-readiness or security certification.

## Deadline and working rules

- The original submission window is three hours. Do not reset that deadline when execution starts; subtract review and planning time already spent.
- Reserve the final 40 minutes for rehearsal, recording, link checks, and submission. The elapsed targets below describe a maximum 140-minute engineering sequence followed by that reserve; compress against the actual time remaining.
- Keep all three new features small: existing server data, no new payment authority, no speculative infrastructure, and no dependency unless necessary.
- If infrastructure recovery overruns, continue independent UI/documentation work and report the concrete blocker promptly. Do not silently replace live acceptance with fabricated results.
- At the recording cutoff, stop adding scope. Report incomplete work explicitly and use clearly dated historical evidence only where appropriate.
- Passkey presence is performed by the human. Keep credentials off camera. Prepare payment and submission steps for the user rather than assuming their completion.

## Goals and completion criteria

### G1 — Close the catalog administration boundary

Priority: P0. Target: first 25 minutes. Dependencies: none.

Trust boundary: catalog price, schema, status, and refund-term mutation belongs to trusted administration, never to anonymous callers or the buyer agent.

- [ ] Inspect the existing operator roles and choose the smallest coherent administration policy.
- [ ] Guard merchant/service POST and PATCH routes with explicit administrative authorization plus the existing authenticated-mutation protections. Do not grant every operator role write authority automatically.
- [ ] If a safe administrative policy cannot be completed within the timebox, disable these HTTP writes in the presentation deployment; seed/configure through trusted local scripts. Document the limitation.
- [ ] Preserve public catalog discovery and the intended public quote path.
- [ ] Verify deployment proxy rules do not expose an alternate unguarded management path.
- [ ] Add meaningful rejection tests for anonymous and ordinary-buyer writes; test permitted administration if enabled, malformed requests, CSRF/Origin handling, and continued catalog access.
- [ ] Update the README's current local-admin caveat to match final behavior.

Primary files: `apps/api/app/api/v1/merchants.py`, `services.py`, `dependencies.py`, relevant API tests, deployment configuration.

Done: unauthorized calls cannot change catalog terms; intended buyer discovery and purchasing still work.

### G2 — Establish a reproducible demo runtime

Priority: P0. Target: minutes 25–45. Dependencies: G1 before exposing writes publicly.

- [ ] Restore Docker availability without deleting or resetting existing data.
- [ ] Confirm the configured PostgreSQL port, Redis, migrations, and seeded catalog.
- [ ] Start API, web, OrbitIntel, webhook worker, entitlement worker, and refund worker; inspect actual worker/readiness evidence.
- [ ] Build the web app with the intended `NEXT_PUBLIC_API_URL`; a root dotenv file is not proof the frontend build consumed it.
- [ ] Verify RP ID, frontend origin, cookies, CORS, and MCP handoff URL agree on the actual demo host. Do not interchange `localhost` and `127.0.0.1` during passkey acceptance.
- [ ] Confirm Razorpay Test Mode configuration and the webhook destination without printing secret values.
- [ ] Confirm a real CelesTrak-backed merchant response or record an upstream failure honestly.
- [ ] Make startup instructions reproducible and add an actionable preflight check where practical. Avoid forcing an unused tunnel implementation.

Done: a browser on the presentation hostname can reach the real catalog and account flow; required services and workers are healthy. Payment/fulfillment readiness is checked separately from basic process health.

### G3 — Make delivered value readable

Priority: P1. Target: minutes 45–65. Dependencies: existing fulfillment contract; live acceptance depends on G2.

- [ ] Add a typed result presentation for each supported OrbitIntel result type.
- [ ] Display identity, relevant orbital values with units, source epoch/freshness, and the existing interpretation and limitations where available.
- [ ] Render only a successfully delivered result from the existing authorized flow.
- [ ] Keep the exact raw JSON and result hash available in an expandable evidence section.
- [ ] Handle unknown/malformed result shapes safely without inventing missing data or crashing the purchase screen.
- [ ] Verify desktop/mobile readability, long values, and unknown-result fallback.

Primary area: `apps/web/components/paid-resource-access.tsx` and a small reusable report component.

Done: judges can understand what was purchased without reading a JSON payload; the report preserves backend facts and CelesTrak limitations.

### G4 — Add a downloadable purchase receipt

Priority: P1. Target: minutes 65–80. Dependencies: existing authenticated transaction evidence; G3 for result display integration.

- [ ] Define an explicit allowlist of receipt fields; do not serialize entire internal objects.
- [ ] Include available merchant/service identity, transaction and quote references, integer amount/currency, purchase type, policy/approval evidence, payment state, fulfillment/refund state, timestamps, and result hash.
- [ ] Show unavailable evidence as unavailable; never infer successful fulfillment from captured payment.
- [ ] Offer a simple JSON download from already authorized server responses. Add a readable/printable view only if it fits without another dependency.
- [ ] Label it a Test Mode purchase receipt/evidence snapshot, with export time; do not claim it is a tax invoice, signed attestation, or independently verified document.
- [ ] Exclude capability/session/CSRF tokens, signatures, provider secrets, raw webhook payloads, and unnecessary personal data.
- [ ] Test field allowlisting and representative fulfilled, pending, and refunded evidence. Preserve ownership checks if any new API is required.

Done: a buyer can export a useful receipt whose states agree with the displayed authoritative evidence and which contains no credentials.

### G5 — Add guided scenarios and improve first impressions

Priority: P1. Target: minutes 80–100. Dependencies: established routes and existing MCP prompts.

- [ ] Put a concise demo entry directly below the hero, with clear steps for connecting the agent and reviewing a purchase.
- [ ] Add three copyable prompts: purchase within budget, reject an explicitly requested over-budget service, and observe recovery after a deliberately faulted fulfillment.
- [ ] Explain preconditions and human handoff; distinguish prompt instructions from executed outcomes.
- [ ] Keep fault injection a deliberate development/Test Mode operator setup. Do not add a public failure/refund trigger.
- [ ] Replace the five empty public metric tiles with dated, scoped evidence that can be traced to repository artifacts. Do not expose the operator dashboard's private records publicly.
- [ ] Make successful result, policy denial, and recovery states visually clear. Keep technical evidence available without making every buyer read it.
- [ ] Verify anchor links, keyboard focus, copying, disabled/error states, and responsive layout.

Done: a judge can find and understand the three scenarios immediately; all progress reflects actual backend state.

### G6 — Prepare credible evidence and merchant documentation

Priority: P1. Target: minutes 100–115. Dependencies: final implemented contracts; live result collection depends on G2.

- [ ] Label the 60-case harness as a deterministic policy evaluation.
- [ ] Correct any wording implying that oversized-quote tests are a complete LLM prompt-injection evaluation.
- [ ] Distinguish external Codex reasoning from the bundled keyword-based reference planner.
- [ ] Prepare a small agent-run evidence table with prompt, expected service/outcome, actual selection, policy outcome, terminal commerce state, timestamps, and evidence references. Leave unexecuted rows pending.
- [ ] Record success, denial, and refund recovery runs when actually performed. Keep historical refund evidence explicitly dated and its timing interval correctly defined.
- [ ] Add a merchant integration quickstart describing the real private fulfillment request/response, shared-secret boundary, input/output schemas, idempotency key, configuration, quote, 402, and failure semantics.
- [ ] Refresh the documentation index and README entry points. Add the final demo/video URLs only after they exist.
- [ ] Explain protocol inspiration accurately; make no unverified ACP/AP2/x402 conformance claim.

Done: every visible claim has an accurate scope and source; a developer can understand how a second merchant would integrate without assuming a finished self-service portal or SDK.

### G7 — Final verification and presentation package

Priority: P0. Target: minutes 115–140, then final 40-minute presentation reserve. Dependencies: G1–G6 for their respective acceptance checks.

- [ ] Run focused tests for changed authority and receipt logic, then the appropriate API, merchant, MCP, and frontend checks.
- [ ] Run database integration/migration checks against isolated test schemas on the intended test database. Do not delete existing commerce evidence.
- [ ] Build the production frontend with the correct API origin and verify it visually.
- [ ] Rehearse successful purchase → verified fulfillment → report → receipt.
- [ ] Rehearse explicit policy denial, with no subsequent approval/payment/value release.
- [ ] Rehearse paid fulfillment failure → compensation → provider-confirmed processed refund. Verify existing retry/idempotency checks and evidence continuity.
- [ ] Verify an exact repeated resource request returns the stored result without a second logical purchase/execution; describe this behavior accurately.
- [ ] Export sanitized evidence and inspect it for credentials and unnecessary private fields.
- [ ] Run lint/typecheck/build, relevant tests, secret scan, Compose validation, and `git diff --check`. Inspect intended untracked files and preserve unrelated edits.
- [ ] Prepare a five-minute script, screen order, short architecture diagram, and likely judge questions with factual answers.
- [ ] User completes passkey prompts, on-camera narration, video upload, and the final submission. Confirm public repository/video links work for a signed-out judge.
- [ ] End with an explicit readiness report listing completed items, failed/skipped checks, live versus historical evidence, and any remaining user steps.

Done: the demo and evidence establish successful commerce, bounded rejection, and graceful recovery; the submission has usable links and makes no unsupported claims. Do not mark the overall active goal complete while required acceptance or presentation work remains.

## Five-minute presentation

### Added user priority: configure MCP once, renew access

- [x] Preserve the existing configured key across access renewals; no new key or client registration per conversation.
- [x] Add owner-only renewal with Origin/CSRF checks and a one-use passkey proof bound to the exact connection, existing scopes, and requested lifetime.
- [x] Preserve expiry, bounded lifetime, and permanent revocation. Allow revoked-key replacement only through new setup.
- [x] Add 15-minute, 30-minute, and one-hour access choices; link expired MCP errors to browser renewal.
- [x] Verify the original token works after renewal in an isolated PostgreSQL integration test; test cross-owner access, proof mismatch/replay, rollback, stale revocation metadata, and anonymous/bearer-only rejection.
- [x] Run backend checks (804 passed, 4 skipped), frontend tests/typecheck/build, and update setup/security guidance.
- [ ] Complete the human passkey renewal against the configured live connection and observe the same MCP registration succeed. Its owner is the existing “Festin” account; the current browser buyer “fest” is a different account.

### Recording sequence

| Time | Screen and point |
| --- | --- |
| 0:00–0:25 | Merchant problem and product promise on the homepage. |
| 0:25–1:15 | Codex compares the real catalog, proposes a service, and receives authoritative policy checks. |
| 1:15–2:30 | Human approval, Razorpay Test Mode payment, verified delivered report, and receipt. |
| 2:30–3:00 | An explicitly requested ₹9 service is rejected under a ₹5 maximum. |
| 3:00–4:20 | Deliberately failed delivery releases no result; backend compensation reaches processed refund evidence. |
| 4:20–5:00 | Audit trail, accurately scoped measurements, and architecture. |

Opening: “MeterGate turns a paid API into a purchase an AI buyer can complete. The agent chooses, the user approves, Razorpay processes the payment, and MeterGate verifies delivery or handles compensation.”

Likely judge questions: Where does AI add value? Why is the payment bounded? What if a webhook is duplicated? What if the merchant fails? What prevents repeat resource execution? How does another merchant integrate? Which measurements are live, historical, or synthetic? What remains before production?

## Baseline from the preceding review

These are review-time results, not acceptance for future edits:

| Check | Observed result |
| --- | --- |
| Backend without database-dependent/live Razorpay suites | 697 passed, 2 skipped |
| OrbitIntel without live CelesTrak integration | 70 passed |
| MCP | 16 passed; typecheck/build passed |
| Frontend | 7 source-contract checks passed; lint/build passed |
| Deterministic policy matrix | 60/60 passed |
| Secret scan, Compose validation, diff whitespace | Passed |
| Database integration | Configured database connection refused; not accepted |
| Live browser commerce | Not executed in the review |
| Plain production web build | Compiled, but API URL was absent at runtime |

## Follow-on product goals

These capture the longer-term ideas mentioned in the review; they are not silently included in a three-hour shipping promise.

1. Merchant self-service onboarding: ownership model, protected administration, schema/endpoint validation, an integration test, and a second real merchant. Completion requires cross-merchant authorization tests and verified fulfillment.
2. In-app AI buyer: model-provider configuration, strict proposal schemas, a visible catalog comparison, session-scoped conversation, and the existing approval/payment handoff. Completion requires malformed/injected-output tests and measured agent runs.
3. Budget-aware alternatives: when substitution is allowed, propose a suitable affordable service after denial; create a new quote and reevaluate policy. Never raise the budget, mutate an approved quote, or substitute against explicit user intent.

Subscriptions, unattended real-money purchasing, additional payment rails, and new protocol compatibility remain outside this submission scope.
