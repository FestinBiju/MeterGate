# Demo Preparation and Evidence

Set `DEMO_MODE=true` only for a dedicated development/staging account, then run:

```powershell
Set-Location apps/api
uv run python -m app.scripts.prepare_demo acct_… --confirm
```

This idempotently restores the OrbitIntel catalog and revokes active MCP sessions for that account. It does not delete payments, audit events, entitlements, fulfillments, compensation, or refunds.

Export one sanitized transaction:

```powershell
uv run python -m app.scripts.export_transaction_evidence txn_… --output ../../docs/evidence/demo-transaction.json
```

The export contains IDs, hashes, reason codes and state transitions, never session/MCP/capability secrets or passkey/Razorpay secret material.
