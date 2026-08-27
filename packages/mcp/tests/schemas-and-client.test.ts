import assert from "node:assert/strict";
import test from "node:test";

import { MeterGateApiClient, MeterGateApiError } from "../src/api-client.js";
import { boundedJson, policyInput, quoteInput } from "../src/schemas.js";

const token = `mcp_${"a".repeat(43)}`;
const serviceId = `svc_${"0".repeat(26)}`;

test("strict quote schema rejects agent-selected money and unknown fields", () => {
  assert.throws(
    () =>
      quoteInput.parse({
        service_id: serviceId,
        input: { norad_id: 25544 },
        amount: 1,
        currency: "INR",
      }),
    /unrecognized/i,
  );
});

test("bounded JSON rejects excessive depth and size", () => {
  let nested: unknown = null;
  for (let index = 0; index < 34; index += 1) nested = [nested];
  assert.equal(boundedJson.safeParse(nested).success, false);
  assert.equal(boundedJson.safeParse("x".repeat(262_145)).success, false);
});

test("agent policies are explicitly one-time only", () => {
  const base = {
    maximum_amount: 1_000,
    expires_in_seconds: 900,
  };
  assert.equal(
    policyInput.safeParse({ ...base, allowed_purchase_types: ["one_time"] }).success,
    true,
  );
  assert.equal(
    policyInput.safeParse({ ...base, allowed_purchase_types: ["subscription"] }).success,
    false,
  );
  assert.equal(policyInput.safeParse(base).success, false);
});

test("API client restricts destinations and tool paths", async () => {
  assert.throws(
    () => new MeterGateApiClient("http://example.com/api/v1/", token),
    /HTTPS or an HTTP loopback/,
  );
  assert.throws(
    () => new MeterGateApiClient("https://user:pass@example.com/api/v1/", token),
    /HTTPS or an HTTP loopback/,
  );
  const client = new MeterGateApiClient("http://localhost:8000/api/v1/", token);
  await assert.rejects(() => client.call("/operator/incidents", {}), /not allowlisted/);
});

test("API failures never disclose bridge token or provider response bodies", async () => {
  const fetcher: typeof fetch = async () =>
    new Response(
      JSON.stringify({
        detail: { code: "MCP_SCOPE_DENIED", internal: token },
        provider_secret: "razorpay-secret",
      }),
      { status: 403, headers: { "content-type": "application/json" } },
    );
  const client = new MeterGateApiClient(
    "http://localhost:8000/api/v1/",
    token,
    fetcher,
  );

  await assert.rejects(
    () => client.call("/mcp/tools/list-services", {}),
    (error: unknown) => {
      assert.ok(error instanceof MeterGateApiError);
      assert.equal(error.code, "MCP_SCOPE_DENIED");
      assert.equal(error.message, "MeterGate rejected the scoped MCP tool call.");
      assert.equal(JSON.stringify(error).includes(token), false);
      assert.equal(String(error).includes("razorpay-secret"), false);
      return true;
    },
  );
});

test("API client sends the bridge token only in Authorization", async () => {
  let observedUrl = "";
  let observedBody = "";
  let observedAuthorization = "";
  const fetcher: typeof fetch = async (input, init) => {
    observedUrl = String(input);
    observedBody = String(init?.body ?? "");
    observedAuthorization = new Headers(init?.headers).get("authorization") ?? "";
    return new Response(JSON.stringify({ merchants: [] }), { status: 200 });
  };
  const client = new MeterGateApiClient(
    "http://localhost:8000/api/v1/",
    token,
    fetcher,
  );
  await client.call("/mcp/tools/list-services", {});

  assert.equal(observedAuthorization, `Bearer ${token}`);
  assert.equal(observedUrl.includes(token), false);
  assert.equal(observedBody.includes(token), false);
});
