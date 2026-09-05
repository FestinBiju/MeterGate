import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function importTypescript(path) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
}
const { buildPurchaseReceipt } = await importTypescript("../lib/purchase-receipt.ts");
const { describeOrbitResult } = await importTypescript("../lib/orbit-report.ts");
const transaction = {
  transaction_id: "txn_00000000000000000000000001", authorization_id: "auth_test",
  merchant: { id: "merchant", name: "OrbitIntel", token: "do-not-export" },
  service: { id: "service", name: "Report" }, amount: 500, currency: "INR", purchase_type: "one_time",
  state: "paid", commerce_outcome: "fulfilled", provider_order_id: "order_test", paid_at: "2026-09-05T00:00:00Z",
  checkout: { key_id: "do-not-export" }, token: "do-not-export", csrf_token: "do-not-export",
};
const authorization = { id: "auth_test", evaluation_id: "eval_test", merchant: transaction.merchant, service: transaction.service, amount: 500, currency: "INR", quote_hash: "sha256:test", authorized_at: "2026-09-05T00:00:00Z" };
const evaluation = { id: "eval_test", quote_id: "quote_test", policy_id: "policy_test", decision: "allow", checks: [{ rule: "MAX_AMOUNT", result: "pass", reason_code: "WITHIN_LIMIT", details: { token: "do-not-export" } }] };
const result = { transaction_id: transaction.transaction_id, result_hash: `sha256:${"a".repeat(64)}`, completed_at: "2026-09-05T00:00:05Z", result: { token: "do-not-export" } };

test("receipt exports only allowed fields and integer monetary evidence", () => {
  const receipt = buildPurchaseReceipt(transaction, authorization, evaluation, result, "2026-09-05T00:01:00Z");
  assert.equal(receipt.amount_minor, 500);
  assert.equal(receipt.fulfillment.result_hash, result.result_hash);
  assert.equal(receipt.policy_checks[0].result, "pass");
  assert.doesNotMatch(JSON.stringify(receipt), /do-not-export|csrf_token|checkout|key_id/);
});
test("pending payment never becomes delivered value in a receipt", () => {
  const receipt = buildPurchaseReceipt({ ...transaction, state: "payment_pending", commerce_outcome: "payment_pending" }, authorization, evaluation, result, "now");
  assert.equal(receipt.payment.state, "payment_pending");
  assert.equal(receipt.fulfillment, null);
});
test("refunded receipt preserves paid history, provider status, and withholds result", () => {
  const receipt = buildPurchaseReceipt({ ...transaction, commerce_outcome: "refunded", refund_summary: { refund_id: "refund_test", amount: 500, currency: "INR", state: "refunded", provider_status: "processed", provider_refund_id: "rfnd_test", token: "do-not-export" } }, authorization, evaluation, result, "now");
  assert.equal(receipt.payment.state, "paid");
  assert.equal(receipt.refund.provider_status, "processed");
  assert.equal(receipt.fulfillment, null);
  assert.doesNotMatch(JSON.stringify(receipt), /do-not-export/);
});
test("receipt rejects mismatched purchase evidence and fractional minor units", () => {
  for (const altered of [{ ...transaction, amount: 500.5 }, { ...transaction, authorization_id: "another" }, { ...transaction, merchant: { id: "another", name: "Different" } }]) {
    assert.throws(() => buildPurchaseReceipt(altered, authorization, evaluation, result, "now"));
  }
  assert.throws(() => buildPurchaseReceipt(transaction, authorization, { ...evaluation, id: "another" }, result, "now"));
  assert.throws(() => buildPurchaseReceipt(transaction, authorization, evaluation, { ...result, transaction_id: "another" }, "now"));
  assert.throws(() => buildPurchaseReceipt(transaction, authorization, evaluation, { ...result, result_hash: "not-a-hash" }, "now"));
});
test("reports support status, risk, and nested detailed contracts with units", () => {
  const source = { object_name: "ISS", norad_id: 25544, epoch: "2026-09-05T00:00:00Z", inclination: 51.6 };
  const status = describeOrbitResult({ ...source, result_type: "satellite_status", mean_motion: 15.5, data_source: "CelesTrak" });
  assert.equal(status.identity, "ISS · NORAD 25544");
  assert.ok(status.fields.some(field => field.value === "15.5 rev/day"));
  const risk = describeOrbitResult({ ...source, result_type: "orbital_risk_report", approximate_perigee_altitude_km: 400, very_low_perigee_indicator: false });
  assert.ok(risk.fields.some(field => field.value === "400 km"));
  assert.ok(risk.fields.some(field => field.value === "Not flagged by heuristic"));
  const detail = describeOrbitResult({ result_type: "detailed_orbital_analysis", source, derived: { orbital_period_minutes: 92 }, freshness: { data_freshness_indicator: "fresh" }, disclaimer: "Heuristic only" });
  assert.ok(detail.fields.some(field => field.value === "92 min"));
  assert.equal(detail.disclaimer, "Heuristic only");
});
test("unknown or malformed reports fall back without invented measurements", () => {
  for (const value of [null, [], {}, { result_type: "constructor" }, { result_type: "satellite_status", norad_id: "wrong" }]) assert.equal(describeOrbitResult(value), null);
  const report = describeOrbitResult({ result_type: "satellite_status", norad_id: 25544, object_name: "ISS", mean_motion: NaN });
  assert.deepEqual(report.fields, []);
});
test("public policy evidence is exactly the generated artifact", async () => {
  const publicEvidence = JSON.parse(await readFile(new URL("../lib/policy-evidence.json", import.meta.url), "utf8"));
  const generated = JSON.parse(await readFile(new URL("../../../docs/evaluation/results.json", import.meta.url), "utf8"));
  assert.deepEqual(publicEvidence, generated);
});
