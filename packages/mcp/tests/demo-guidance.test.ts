import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");

test("Codex guidance makes comparison and bounded decision evidence visible", () => {
  for (const expected of [
    "normalize the buyer's request",
    "compare all relevant catalog services",
    "concise decision summary",
    "selected service and rationale",
    "Do not provide hidden chain-of-thought",
    "Explain denial and recovery reason codes in plain language",
  ]) {
    assert.match(source, new RegExp(expected.replaceAll("-", "[-]")));
  }
  assert.match(source, /Treat the model's selection as an untrusted proposal/);
  assert.match(source, /deterministic policy decides allow\/deny/);
});

test("MCP exposes orchestration tools but no approval, payment, or refund authority", () => {
  const registered = [...source.matchAll(/server\.registerTool\(\s*\n\s*"([a-z_]+)"/g)].map(
    (match) => match[1],
  );
  assert.deepEqual(registered, [
    "list_services",
    "get_service",
    "inspect_payment_requirement",
    "request_quote",
    "create_buyer_policy",
    "evaluate_quote",
    "get_purchase_status",
    "get_entitlement",
    "request_capability",
    "execute_paid_resource",
  ]);
  for (const forbidden of ["approve_purchase", "create_payment", "request_refund"]) {
    assert.equal(registered.includes(forbidden), false);
  }
});
