# Demonstration Workflow

The exact five-minute recording sequence, prompts, measured stats, protocol positioning, and failure-recovery acceptance checklist are in [`judge-readiness.md`](./judge-readiness.md).

Stable request: “Get an orbital risk report for NORAD 25544. Spend no more than ₹10. One-time only.” The reference buyer discovers OrbitIntel’s ₹5 report, requests an immutable quote, creates the bounded policy, and stops at human approval. The human uses the trusted browser and passkey; Razorpay Test Checkout follows. The backend verifies captured state, issues the exact entitlement, and the agent retries with the one-use capability. OrbitIntel retrieves current CelesTrak data; no report is hardcoded.

For PoHP, create an MCP session. MeterGate shows “Human verification required,” invokes Windows Hello/passkey user verification, issues an action-bound proof, and consumes it into that exact scope/lifetime. No image puzzle appears.

With `DEMO_MODE=true`, the authenticated operator dashboard adds a Hackathon agent metrics panel. Its GMV, successful purchases, policy denials, handled payment failures, fulfillment successes, refund recoveries, and average access latency are joined from MCP audit artifact IDs to authoritative commerce records. Empty evidence renders as zero; the UI does not synthesize activity.

For the graceful-failure demonstration, enable the documented development-only OrbitIntel permanent fault mode, complete a Test Mode payment, observe that no result crosses the boundary, then follow compensation and refund evidence through the operator dashboard. Production/staging fault injection remains disabled unless explicitly configured in an approved demo environment.
