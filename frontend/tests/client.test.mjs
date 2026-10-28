import assert from "node:assert/strict";
import { test } from "node:test";
import { apiJson, formatDetail } from "../src/api/client.js";

test("string details are shown as-is", () => {
  assert.equal(formatDetail("target resolves to a non-public IP address", "fallback"), "target resolves to a non-public IP address");
});

test("FastAPI validation lists become readable text, not [object Object]", () => {
  const detail = [{ loc: ["body", "authorization_confirmed"], msg: "Value error, authorization must be confirmed before a run can be requested" }];
  assert.equal(formatDetail(detail, "fallback"), "authorization_confirmed: authorization must be confirmed before a run can be requested");
});

test("missing details use the fallback", () => {
  assert.equal(formatDetail(undefined, "fallback"), "fallback");
  assert.equal(formatDetail([], "fallback"), "fallback");
});

test("non-2xx responses throw the formatted detail", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: [{ loc: ["body", "url"], msg: "Input should be a valid URL" }] }), { status: 422 });
  await assert.rejects(apiJson("/api/tests/website", { method: "POST", body: {} }), /url: Input should be a valid URL/);
});

test("network failures give an actionable message", async () => {
  globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
  await assert.rejects(apiJson("/api/health"), /Could not reach the TestPilot API/);
});
