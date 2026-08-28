# Track 01 Evaluation Methodology

Run from `apps/api`:

```powershell
uv run python -m app.scripts.evaluate_milestone11
```

The harness executes 60 fresh scenarios through the production deterministic policy engine: valid purchases, over-budget attempts, wrong currencies, merchants and services, disallowed subscriptions, expired quotes, and prompt-injection-shaped ₹999 attempts. It writes machine-produced `results.json` and `results.md` with the commit hash, timestamp, reason codes, pass/fail status, policy bypass/false-block rates, and an in-process policy latency baseline.

Metrics requiring real Razorpay, entitlement, fulfillment, or refund records remain `null` unless staging evidence is supplied; they are never invented. The prompt-injection claim is deliberately narrow: catalog text cannot override deterministic money policy. It is not a claim of general prompt-injection immunity.
