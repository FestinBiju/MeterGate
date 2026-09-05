import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const connections = await readFile(
  new URL("../components/agent-connections.tsx", import.meta.url),
  "utf8",
);
const handoff = await readFile(
  new URL("../components/agent-purchase-handoff.tsx", import.meta.url),
  "utf8",
);

test("agent sessions are explicit, short-lived, shown once, and revocable", () => {
  assert.match(connections, /Create Agent Session/);
  assert.match(connections, /expires_in_seconds: lifetime/);
  assert.match(connections, /value=\{900\}/);
  assert.match(connections, /value=\{1800\}/);
  assert.match(connections, /value=\{3600\}/);
  assert.match(connections, /Renew access with passkey/);
  assert.match(connections, /actionClass="renew_agent_session"/);
  assert.match(connections, /agent_session_id: pendingAction.session.id/);
  assert.match(connections, /Copy this credential now/);
  assert.match(connections, /Dismiss secret/);
  assert.match(connections, /Revoke Agent Session/);
  assert.match(connections, /Explicit confirmation required/);
  assert.match(connections, /Confirm Agent Session/);
  assert.match(connections, /Confirm Revocation/);
  assert.doesNotMatch(connections, /localStorage|sessionStorage/);
});

test("agent scope selector contains buyer orchestration only", () => {
  for (const scope of [
    "mcp:catalog.read",
    "mcp:quote.create",
    "mcp:policy.create",
    "mcp:policy.evaluate",
    "mcp:commerce.read",
    "mcp:capability.issue",
    "mcp:resource.execute",
  ]) assert.match(connections, new RegExp(scope.replaceAll(".", "\\.")));
  for (const forbidden of ["operator.", "admin.", "refund.force", "payment.force"])
    assert.doesNotMatch(connections, new RegExp(forbidden.replaceAll(".", "\\.")));
});

test("agent handoff reads authoritative evidence and reuses trusted approval UI", () => {
  assert.match(handoff, /policy-evaluations/);
  assert.match(handoff, /TrustedApproval/);
  assert.match(handoff, /Server price/);
  assert.match(handoff, /const allowed = handoff\.decision === "allow"/);
  for (const evidence of [
    "Agent proposal",
    "Normalized input",
    "Immutable quote",
    "Deterministic checks",
    "Buyer maximum",
    "Policy evaluation checks",
    "Human authority",
    "No approval or payment path was created",
  ]) assert.match(handoff, new RegExp(evidence));
  for (const forbidden of ["approve_purchase", "create_razorpay_order", "/refunds", "/operator"])
    assert.doesNotMatch(handoff, new RegExp(forbidden));
});
