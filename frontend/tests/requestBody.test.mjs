import assert from "node:assert/strict";
import { test } from "node:test";
import { apiBody, cleanSteps, formProblems, operationToForm, parseExpected, planToForm, websiteBody } from "../src/lib/requestBody.js";

const base = {
  url: "https://demo.example/", authorized: true, checks: [], expectedText: "", clickSelector: "",
  flowSteps: [], screenshotOnFailure: false, expectedStatusCode: "200", expectedContentType: "application/json",
  maxResponseTimeMs: "5000", headers: [], assertions: [], schemaText: "",
};

test("flow steps keep only the fields their action uses", () => {
  assert.deepEqual(cleanSteps([
    { action: "click", selector: " #go ", value: "ignored" },
    { action: "press", selector: "", value: "Enter" },
    { action: "expect_text", selector: "ignored", value: "Done" },
  ]), [{ action: "click", selector: "#go" }, { action: "press", value: "Enter" }, { action: "expect_text", value: "Done" }]);
});

test("website body only sends inputs for selected checks", () => {
  const body = websiteBody({ ...base, checks: ["h1"], expectedText: "x", clickSelector: "#a" });
  assert.equal(body.expected_text, undefined);
  assert.equal(body.click_selector, undefined);
  const flow = websiteBody({ ...base, checks: ["user_flow"], flowSteps: [{ action: "expect_url", selector: "", value: "/done" }] });
  assert.deepEqual(flow.flow_steps, [{ action: "expect_url", value: "/done" }]);
});

test("expected values are parsed as JSON scalars where that is what the user typed", () => {
  assert.equal(parseExpected("equals", "7"), 7);
  assert.equal(parseExpected("equals", "true"), true);
  assert.equal(parseExpected("equals", "null"), null);
  assert.equal(parseExpected("equals", "Ada"), "Ada");
  assert.equal(parseExpected("contains", "42"), "42");
  assert.equal(parseExpected("length", "3"), 3);
  assert.equal(parseExpected("exists", "anything"), null);
});

test("api body carries headers, assertions and schema", () => {
  const body = apiBody({
    ...base,
    checks: ["api_status", "api_json_assertions", "api_json_schema"],
    headers: [{ name: "Authorization", value: "Bearer t" }, { name: " ", value: "dropped" }],
    assertions: [{ path: "data.id", operator: "equals", expected: "7" }, { path: "", operator: "exists", expected: "" }],
    schemaText: "{\"type\":\"object\"}",
  });
  assert.deepEqual(body.request_headers, { Authorization: "Bearer t" });
  assert.deepEqual(body.json_assertions, [{ path: "data.id", operator: "equals", expected: 7 }]);
  assert.deepEqual(body.response_schema, { type: "object" });
});

test("form problems explain what is missing before a run", () => {
  assert.deepEqual(formProblems("website", { ...base, checks: ["user_flow"], flowSteps: [{ action: "click", selector: "", value: "" }] }), [
    "Step 1 needs a CSS selector.",
    "A flow needs an Expect text or Expect URL step to decide pass or fail.",
  ]);
  assert.equal(formProblems("api", { ...base, checks: ["api_json_schema"], schemaText: "{oops" }).length, 1);
  assert.deepEqual(formProblems("api", { ...base, checks: ["api_status"] }), []);
});

test("a reviewed Gemma plan and an OpenAPI operation fill the form", () => {
  const update = planToForm({
    checks: ["text_present", "api_status", "user_flow"],
    suggested_expected_text: "Email is required",
    suggested_flow_steps: [{ action: "click", selector: "#sign-in-button", value: null }],
  }, ["text_present", "user_flow"]);
  assert.deepEqual(update.checks, ["text_present", "user_flow"]);
  assert.equal(update.expectedText, "Email is required");
  assert.deepEqual(update.flowSteps, [{ action: "click", selector: "#sign-in-button", value: "" }]);

  const op = operationToForm({ url: "https://api.example.com/pets", expected_status_code: 200, expected_content_type: "application/json", response_schema: { type: "array" } });
  assert.deepEqual(op.checks, ["api_status", "api_response_time", "api_content_type", "api_json_schema"]);
  assert.equal(JSON.parse(op.schemaText).type, "array");
});

test("full scan covers every input-free website check and keeps active probes opt-in", async () => {
  const { FULL_SCAN, WEBSITE_CHECKS, API_CHECKS } = await import("../src/lib/requestBody.js");
  assert.equal(WEBSITE_CHECKS.length, 17);
  assert.equal(API_CHECKS.length, 9);
  assert.equal(FULL_SCAN.length, 14);
  assert.ok(!FULL_SCAN.includes("active_probes"));
  assert.ok(FULL_SCAN.includes("cookie_flags") && FULL_SCAN.includes("openapi_contract"));
});

test("load-time limit and JSON sample are sent only for their checks", () => {
  const site = websiteBody({ ...base, checks: ["response_time"], maxLoadTimeMs: "3000" });
  assert.equal(site.max_load_time_ms, 3000);
  assert.equal(websiteBody({ ...base, checks: ["h1"] }).max_load_time_ms, undefined);
  const api = apiBody({ ...base, checks: ["api_json_shape"], sampleText: '{"id": 1}' });
  assert.deepEqual(api.expected_json_example, { id: 1 });
  assert.ok(formProblems("api", { ...base, checks: ["api_json_shape"], sampleText: "{bad" })[0].startsWith("The sample JSON is not valid"));
});
