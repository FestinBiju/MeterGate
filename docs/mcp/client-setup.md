# Local MCP client setup

Prerequisites are a running MeterGate API and web app, an upgraded/seeded database, Node.js 20.9+, and a buyer account with a passkey.

1. Sign in at `http://localhost:3000`.
2. In **Agent Connections**, select only the scopes needed and choose **Create Agent Session**. Confirm the explicit browser prompt.
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

7. Revoke the session in **Agent Connections** when finished. Revocation is immediate; create a new short-lived session to rotate the credential.

The MCP process must inherit the two environment values, but the model should never repeat either a bridge token or capability in conversation. Never place Razorpay, webhook, database, browser-cookie, CSRF, passkey, or OrbitIntel secrets in an MCP client configuration.

