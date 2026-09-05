# Track 01 Evaluation Methodology

Run from `apps/api`:

```powershell
uv run python -m app.scripts.evaluate_milestone11
```

The harness executes 60 fresh scenarios through the production deterministic policy engine: valid purchases, over-budget attempts, wrong currencies, merchants and services, disallowed subscriptions, expired quotes, and prompt-injection-shaped ₹999 attempts. It writes machine-produced `results.json` and `results.md` with the commit hash, timestamp, reason codes, pass/fail status, policy bypass/false-block rates, and an in-process policy latency baseline.

Metrics requiring real Razorpay, entitlement, fulfillment, or refund records remain `null` unless staging evidence is supplied; they are never invented. The prompt-injection claim is deliberately narrow: catalog text cannot override deterministic money policy. It is not a claim of general prompt-injection immunity.

## Exact scope of the injection-shaped cases

The five cases named `prompt-injection-*` submit an oversized quote to deterministic policy. They contain no model inference or adversarial catalog-text evaluation. The measured outcome demonstrates over-budget proposal rejection, not general LLM prompt-injection resistance. The historical metric key is retained for artifact compatibility; use this precise scope in the presentation.

The harness also writes the identical non-sensitive JSON snapshot to `apps/web/lib/policy-evidence.json` so the public evidence page can be packaged independently. The frontend checks that this snapshot matches the generated artifact.

Agent service selection and complete payment journeys require separate observed runs; record them using `docs/evaluation/agent-runs.md`.
