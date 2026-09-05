# Milestone 11 Evaluation Results

Generated: `2026-08-28T05:30:16.473294+00:00`
Commit: `d34f6c3c834513d8fa8e890e729fc7f72609dcfa`
Working tree dirty: `yes`

Scenarios: **60** — 60 passed, 0 failed.

| Metric | Value |
|---|---:|
| `deterministic_valid_decision_rate` | 1.0000 |
| `policy_bypass_rate` | 0.0000 |
| `false_block_rate` | 0.0000 |
| `prompt_injection_unauthorized_purchase_rate` | 0.0000 |
| `agent_test_gmv` | not available |
| `quote_to_payment_conversion_rate` | not available |
| `payment_to_entitlement_latency_ms` | not available |
| `entitlement_to_fulfillment_latency_ms` | not available |
| `refund_recovery_rate` | not available |

Payment/fulfillment metrics remain unavailable until a staging evidence dataset is supplied; the harness does not fabricate them.

## Performance

Policy evaluation mean: **0.110 ms**; p95: **0.117 ms**.
