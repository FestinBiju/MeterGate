# Five-minute presentation and submission checklist

Use this screen order alongside the detailed [judge runbook](judge-readiness.md). Prepare credentials and operator access off camera. Complete the successful-delivery recording with the merchant in normal mode before recording the deliberate failure segment.

## Opening

“MeterGate turns a paid API into a purchase an AI buyer can complete. The agent chooses, the user approves, Razorpay processes the payment, and MeterGate verifies delivery or handles compensation.”

Explain the merchant benefit in one sentence: a merchant can expose a digital service to AI buyers with explicit pricing, bounded authorization, verified delivery, and a recoverable failure path.

## Screen order and narration

| Time | Show | Say / prove |
| --- | --- | --- |
| 0:00–0:25 | Homepage and guided scenarios | The merchant problem and the full purchase-to-delivery outcome. |
| 0:25–1:15 | Connected Codex comparing the live catalog | The ₹5 report fits a ₹6 limit and the requested report depth. AI proposes; server quote/policy owns terms. |
| 1:15–2:30 | Approval handoff, Test Mode Checkout, delivered report | A human approves the exact purchase. Payment is verified server-side. Read the report and download its receipt. |
| 2:30–3:00 | Explicit ₹9 request under a ₹5 maximum | Deterministic rejection; no payment or value release follows. |
| 3:00–4:20 | A separately prepared paid failure and operator timeline | Payment remains historically paid. No result is released. Backend compensation converges to Razorpay refund status `processed`. |
| 4:20–5:00 | `/evidence`, operator metrics, architecture | Show accurately scoped evidence and explain one merchant integration. |

If combining takes, label the scenarios as separate transactions. Do not edit a pending state into an apparently instantaneous recovery. Historic evidence must remain dated. Never leave fault mode enabled for the success take.

## Rehearsal acceptance

- [ ] Production web build uses the actual API origin. The browser has no `API_NOT_CONFIGURED` message.
- [ ] Passkey RP ID, allowed origin, cookies, and the MCP handoff agree on the exact demo host.
- [ ] Catalog has the expected live merchant/services and server prices.
- [ ] All workers are running and their heartbeats are fresh; PostgreSQL and Redis are healthy.
- [ ] Fresh MCP credential is configured privately; no credential appears in chat/screenshots/video.
- [ ] One successful purchase reaches durable fulfillment and a readable report.
- [ ] The downloaded receipt matches the transaction and contains no credentials.
- [ ] The denied request stops before approval/payment.
- [ ] Failure run shows a withheld result and processed refund with matching provider evidence.
- [ ] Exact resource retry demonstrates stored-result replay without another logical execution.
- [ ] New report, receipt, prompts, and evidence page are readable on desktop and mobile and reachable by keyboard.
- [ ] Record actual IDs/times/outcomes in [agent-run evidence](../evaluation/agent-runs.md).

## Judge questions

**Where does AI add value?** The external AI client understands a fuzzy request, compares catalog scope and price, and proposes a service. The reference CLI is keyword-based. Deterministic code owns commercial decisions and state transitions.

**Why not just call the Razorpay API?** A payment alone does not bind an agent's intent to immutable terms, prove permission, grant a limited resource, verify delivery, or compensate non-delivery. MeterGate connects those steps.

**What if the agent overspends?** The backend evaluates the server quote against bounded policy, then requires human approval. The model cannot change price or override rejection.

**What if delivery fails after capture?** The system withholds value and evaluates the purchased refund policy. Refunds are backend-controlled, auditable, and terminal only on trusted processed evidence.

**What if requests or webhooks repeat?** Durable deduplication, state transitions, one entitlement per transaction, execution claims, and merchant idempotency prevent another logical purchase or resource execution. Network requests may repeat; the system does not claim exactly one HTTP attempt.

**How does a second merchant join?** It implements the private fulfillment/idempotency contract and strict schemas, then receives explicit private transport configuration. Self-service onboarding and a published SDK remain future work; see the integration guide.

**What do the metrics prove?** The 60-case artifact measures deterministic policy behavior. The one historical refund proves one provider acceptance path. Agent runs and current operator metrics must be reported separately. The 18.27-second historical interval starts at refund request, not at purchase.

**Is it production-ready or standards-conformant?** This submission runs Razorpay Test Mode. Its 402/catalog/approval concepts are standards-inspired; it does not claim full protocol conformance, production certification, KYC, or unattended live-money purchasing.

## Submission gate

- [ ] Final public repository contains intended new files and changes, with no `.env`, credentials, databases, or build output.
- [ ] CI or equivalent local checks are recorded against the submitted revision; skipped/live checks are listed honestly.
- [ ] README links to the demo, architecture, integration guide, evidence, and final video.
- [ ] Five-minute video is accessible to a signed-out viewer and audio/text are legible.
- [ ] Hosted demo link works if supplied; otherwise clearly state local setup requirements.
- [ ] Restore normal merchant mode and remove temporary demo credentials after recording.
- [ ] User completes the actual buildathon submission and saves its confirmation.

Human steps: passkey presence, interactive checkout, narration/recording, final video upload, and submission. Prepare these screens and files fully before the user takes over. Do not report those steps completed without observing their outcomes.
