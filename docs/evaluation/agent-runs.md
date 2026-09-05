# Observed agent commerce runs

These rows are acceptance tasks, not reported successes. Fill them from actual connected-agent runs and server evidence. The policy unit matrix and historical refund artifact are separate evidence sources.

## Verified browser acceptance, separate from agent selection

On 2026-09-05, a human completed passkey approval and Razorpay Test Mode checkout for the ₹5 Orbital Risk Report, NORAD 25544, with a ₹5 policy maximum. Policy returned `ALLOW_POLICY_SATISFIED`. Transaction `txn_01M1QWZ4Z460JDFZCWSM0T6KYE` reached paid at 04:25:12Z and fulfillment succeeded at 04:25:24Z. The actual report and downloaded allowlisted receipt were inspected. [Sanitized transaction evidence](success-2026-09-05.json) records the audit chain.

Browser replay at approximately 04:30Z returned `stored replay` with unchanged result hash `sha256:802d2381ff91d455a17a444bfc161b053b1c59a14019cde3be27e82535f7b7c6`. A subsequent read-only database check found one fulfillment execution, `ful_01M1QX0QZZGFBTAYNT8PQW63V9`, with `attempt_count=1`. No linked webhook evidence row was present; payment was verified through the backend checkout/reconciliation path.

This does not establish AI service selection, the planned ₹6 agent budget, or connected-agent replay. The configured MCP tools returned `MCP_SESSION_EXPIRED` during the acceptance attempt; renewal is required before the rows below can be claimed.

## Connected-agent acceptance

An additional browser-driven denial at 04:33:02Z rejected the ₹9 Detailed Orbital Analysis quote under a ₹5 maximum with `DENY_AMOUNT_EXCEEDS_LIMIT`. The UI offered no approval or checkout for it. A read-only database check found zero linked authorizations and zero payment transactions. See [live denial evidence](denial-2026-09-05.json). This verifies the deterministic boundary through the browser, separately from MCP orchestration.

| Scenario | Expected behavior | Observed agent choice | Policy outcome | Final commerce outcome | Evidence / UTC timestamps |
| --- | --- | --- | --- | --- | --- |
| NORAD 25544 report, one-time, ₹6 maximum | Select the suitable ₹5 report; approve/pay/deliver | Pending live run | Pending | Pending | Pending |
| Explicit Detailed Orbital Analysis, ₹5 maximum, no substitution | Reject ₹9 quote; no payment | Pending live run | Pending | Pending | Pending |
| Operator-prepared permanent failure after a ₹5 payment | Withhold resource; backend compensation | Pending live run | Pending | Pending | Pending |
| Exact retry of a successfully delivered resource | Return stored result; no new logical execution | Pending live run | Previously allowed purchase | Pending | Pending |

Record the concise agent comparison, selected service, server quote amount, evaluation reason, transaction ID, fulfillment execution ID, and result hash or refund provider status. Keep credentials, signatures, raw webhook bodies, and passkey material out of this file.

Do not report selection accuracy or recovery percentage from unexecuted rows. If recording is interrupted, mark the actual state and preserve pending/review outcomes rather than rounding them up to success.
