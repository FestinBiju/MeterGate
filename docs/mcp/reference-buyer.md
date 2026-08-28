# Reference buyer agent

`packages/mcp/src/reference-buyer.ts` demonstrates a bounded buyer workflow. The small planner interprets purchase intent and ranks catalog text; every commerce fact and decision comes from MeterGate tools.

Build the package, set the bridge environment only in the current shell, and start a purchase:

```powershell
Set-Location C:\Developer\MeterGate\packages\mcp
$env:METERGATE_API_URL = "http://localhost:8000/api/v1/"
$env:METERGATE_MCP_TOKEN = "<shown-once-agent-session-token>"
npm run buyer-agent -- "Get an orbital risk report for NORAD 25544. Spend no more than ₹10. One-time only."
```

The first run discovers a service, requests a server-priced quote, creates an INR/merchant/service/one-time/₹10 policy, evaluates it, prints a serializable context and approval URL, and stops. It cannot call a passkey or payment tool because neither exists.

After the user completes the browser handoff, resume with the returned context:

```powershell
npm run buyer-agent -- --resume '<context-json>'
```

Pending states return a reason code and server backoff. An expired quote is refreshed once under the same still-valid policy. Payment failure, compensation, manual review, and refund states stop safely. `access_ready` causes entitlement lookup, ephemeral capability issuance, and exact protected-resource execution. A completed execution returns the stored result on a later exact call.

The default planner is deterministic so ordinary tests need no external LLM. An LLM can replace only the `BuyerPlanner` interface to interpret intent or propose a catalog service. It cannot supply price, change the buyer's policy decision, approve, pay, manufacture entitlement, or authorize a refund.

