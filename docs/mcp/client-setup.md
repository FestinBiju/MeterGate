# Local MCP client setup

Prerequisites are a running MeterGate API and web app, an upgraded/seeded database, Node.js 20.9+, and a buyer account with a passkey.

1. Sign in at `http://localhost:3000`.
2. For first-time setup, in **Agent Connections**, select only the scopes needed, choose an access duration (15 minutes, 30 minutes, or one hour), and choose **Create Agent Session**. Confirm with your passkey.
3. Copy the displayed credential immediately. MeterGate will not show it again.
4. Build the server:

   ```powershell
   Set-Location C:\Developer\MeterGate\packages\mcp
   npm install
   npm run build
   ```

5. Register the stdio server with Codex. Replace the placeholder at the terminal; do not commit the resulting local configuration or credential:

   ```powershell
   codex mcp add metergate --env METERGATE_API_URL=http://localhost:8000/api/v1/ --env METERGATE_MCP_TOKEN=<copy-the-shown-once-token> -- npm --prefix C:\Developer\MeterGate\packages\mcp run start
   ```

   Verify with `codex mcp get metergate` or `codex mcp list`. If the API uses another loopback port, change only `METERGATE_API_URL`. Plain HTTP is rejected except for loopback; non-local deployments require HTTPS.

6. Ask the client to use MeterGate's 402-first flow. When it returns `human_approval_required`, open the server-generated URL. The agent must wait while the human completes passkey approval and Razorpay Test Mode Checkout.

   MeterGate represents money as integer currency minor units. For INR, values are paise: a ₹5 quote is `500`, and a ₹10 maximum policy is `1000`. MCP clients must convert rupees to paise before tool calls and convert paise back to rupees only for display.

   For the judge-facing flow, ask Codex to show a concise decision summary rather than hidden chain-of-thought. It should normalize the requested resource/input, budget, currency, and purchase type; compare every relevant service using server-published scope and price; state the selected service and short rationale; then show the structured quote/policy intent. After evaluation or recovery, it should translate authoritative reason codes into plain language without overriding them or inventing success.

7. Keep this client registration for later conversations. When access expires, open **Agent Connections**, find the existing connection, choose **Renew access**, and confirm with your passkey. The same configured token immediately works again; no token copying, client removal, or re-registration is needed. Renewal preserves the exact scopes and grants a fresh bounded window from the renewal time.

8. Use **Revoke Agent Session** when retiring a client or if its key may be exposed. Revocation is permanent, including for expired connections. A revoked or lost key requires a new connection and one-time client configuration.

## Resuming an existing connection

An `MCP_SESSION_EXPIRED` error includes a trusted browser link to the connection. Sign in as its owning buyer, renew it, then ask the agent to retry. An expired connection cannot call tools or renew itself. Renewal requires the signed-in owner's Origin/CSRF-protected request and a one-use passkey proof bound to that connection, its unchanged scopes, and the selected lifetime. Starting a new conversation does not itself require a new key.

If the connection is absent from the list, check that you signed in with the same buyer account that created it. Another account cannot renew it. MeterGate stores only the token hash and cannot recover a lost token.

The MCP process must inherit the two environment values, but the model should never repeat either a bridge token or capability in conversation. Never place Razorpay, webhook, database, browser-cookie, CSRF, passkey, or OrbitIntel secrets in an MCP client configuration.
