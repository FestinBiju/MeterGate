import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../components/operator-dashboard.tsx", import.meta.url),
  "utf8",
);

test("operator dashboard separates facts, derived state, and decisions", () => {
  assert.match(source, />Facts</);
  assert.match(source, />Derived state</);
  assert.match(source, />Operator decision</);
});

test("operator actions are explicit and pass through a confirmation dialog", () => {
  for (const label of [
    "Approve Full Refund",
    "Reject Compensation",
    "Run Payment Reconciliation",
    "Run Refund Reconciliation",
    "Run Fulfillment Reconciliation",
  ]) assert.match(source, new RegExp(label));
  assert.match(source, /Explicit confirmation required/);
  assert.match(source, /X-CSRF-Token/);
  assert.match(source, /Reauthenticate with Passkey/);
  assert.match(source, /\/auth\/reauth\/options/);
  assert.match(source, /\/auth\/reauth\/verify/);
});

test("dashboard renders timeline, alerts, workers, and no credential material", () => {
  assert.match(source, /Unified timeline/);
  assert.match(source, /Worker & outbox health/);
  assert.match(source, />Alerts</);
  for (const forbidden of ["webhook_secret", "key_secret", "session_id", "raw_payload"])
    assert.doesNotMatch(source, new RegExp(forbidden, "i"));
});

test("dashboard shows an evidence-backed recovery ladder with explicit refresh state", () => {
  for (const label of [
    "Payment captured",
    "Fulfillment failed; value withheld",
    "Compensation approved",
    "Refund requested",
    "Refund processed",
    "Refresh selected evidence",
    "Refreshing evidence",
    "Recovery complete: Razorpay refund processed",
  ]) assert.match(source, new RegExp(label));
  assert.match(source, /facts\.refund\?\.state === "refunded" && facts\.refund\.provider_status === "processed"/);
  assert.match(source, /aria-live="polite"/);
  assert.doesNotMatch(source, /setInterval/);
});
