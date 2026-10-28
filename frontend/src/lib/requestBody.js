// Pure helpers that turn form state into API request bodies. Kept free of React
// so they can be unit-tested with node's built-in test runner.

export const FLOW_ACTIONS = [
  ["click", "Click", true, false],
  ["fill", "Type into", true, true],
  ["press", "Press key", false, true],
  ["wait_for", "Wait for element", true, false],
  ["expect_text", "Expect text", false, true],
  ["expect_url", "Expect URL contains", false, true],
];
// [check id, label, category]. Categories drive the dashboard panels.
export const WEBSITE_CHECKS = [
  ["http_status", "HTTP status", "response"], ["response_time", "Load time", "response"],
  ["response_headers", "Response headers", "response"], ["body_not_empty", "Body has content", "response"],
  ["page_title", "Page title", "content"], ["h1", "Main heading", "content"],
  ["console_errors", "Browser console", "content"], ["same_origin_links", "Page links", "content"],
  ["text_present", "Expected text", "content"], ["user_flow", "User flow", "content"],
  ["security_headers", "Security headers", "security"], ["cookie_flags", "Cookie flags", "security"],
  ["exposed_resources", "Exposed files", "security"], ["active_probes", "Active probes (XSS, access)", "security"],
  ["accessibility", "Accessibility (axe WCAG)", "accessibility"], ["keyboard_focus", "Keyboard focus", "accessibility"],
  ["openapi_contract", "OpenAPI contract (Schemathesis)", "contract"],
];
export const API_CHECKS = [
  ["api_status", "Expected status", "response"], ["api_response_time", "Response time", "response"],
  ["api_content_type", "Content type", "response"], ["api_redirects", "No redirects", "response"],
  ["api_response_size", "Response size", "response"], ["api_json_assertions", "JSON fields", "content"],
  ["api_json_schema", "JSON Schema", "content"], ["api_json_shape", "Sample JSON shape", "content"],
  ["openapi_contract", "OpenAPI contract (Schemathesis)", "contract"],
];
// One URL, everything that needs no extra input. Active probes stay opt-in.
export const FULL_SCAN = WEBSITE_CHECKS.map(([key]) => key).filter((key) => !["text_present", "user_flow", "active_probes"].includes(key));
export const CHECK_LABELS = Object.fromEntries([...WEBSITE_CHECKS, ...API_CHECKS].map(([key, label]) => [key, label]));
export const CHECK_CATEGORY = Object.fromEntries([...WEBSITE_CHECKS, ...API_CHECKS].map(([key, , category]) => [key, category]));

export const ASSERTION_OPERATORS = ["exists", "not_exists", "equals", "not_equals", "contains", "type", "length"];

export const emptyStep = () => ({ action: "click", selector: "", value: "" });
export const emptyAssertion = () => ({ path: "", operator: "exists", expected: "" });
export const emptyHeader = () => ({ name: "", value: "" });

export function flowAction(action) {
  const [, label, needsSelector, needsValue] = FLOW_ACTIONS.find(([key]) => key === action) || FLOW_ACTIONS[0];
  return { label, needsSelector, needsValue };
}

export function cleanSteps(steps) {
  return steps.map((step) => {
    const { needsValue } = flowAction(step.action);
    const selector = step.selector.trim();
    const out = { action: step.action };
    // press may target an element; otherwise the selector is only for selector actions.
    if (selector && (flowAction(step.action).needsSelector || step.action === "press")) out.selector = selector;
    if (needsValue) out.value = step.value;
    return out;
  });
}

// "7" -> 7, "true" -> true, "null" -> null, anything else stays a string.
export function parseExpected(operator, raw) {
  if (["exists", "not_exists"].includes(operator)) return null;
  if (["contains", "type"].includes(operator)) return raw;
  const text = raw.trim();
  if (operator === "length") return Number.parseInt(text, 10);
  if (text === "") return "";
  try {
    const value = JSON.parse(text);
    return value === null || ["number", "boolean", "string"].includes(typeof value) ? value : raw;
  } catch {
    return raw;
  }
}

export function cleanAssertions(rows) {
  return rows
    .filter((row) => row.path.trim())
    .map((row) => ({ path: row.path.trim(), operator: row.operator, expected: parseExpected(row.operator, row.expected) }));
}

export function headersObject(rows) {
  return Object.fromEntries(rows.filter((row) => row.name.trim()).map((row) => [row.name.trim(), row.value]));
}

export function parseSchema(text) {
  if (!text.trim()) return { schema: null, error: null };
  try {
    const schema = JSON.parse(text);
    if (!schema || typeof schema !== "object" || Array.isArray(schema)) return { schema: null, error: "The schema must be a JSON object." };
    return { schema, error: null };
  } catch (error) {
    return { schema: null, error: `The schema is not valid JSON: ${error.message}` };
  }
}

export function parseSample(text) {
  if (!text.trim()) return { sample: undefined, error: null };
  try {
    return { sample: JSON.parse(text), error: null };
  } catch (error) {
    return { sample: undefined, error: `The sample JSON is not valid: ${error.message}` };
  }
}

export function websiteBody(form) {
  const checks = form.checks;
  const body = {
    url: form.url,
    authorization_confirmed: form.authorized,
    checks,
    capture_screenshot_on_failure: form.screenshotOnFailure,
  };
  if (checks.includes("text_present")) {
    body.expected_text = form.expectedText;
    if (form.clickSelector.trim()) body.click_selector = form.clickSelector.trim();
  }
  if (checks.includes("user_flow")) body.flow_steps = cleanSteps(form.flowSteps);
  if (checks.includes("response_time")) body.max_load_time_ms = Number(form.maxLoadTimeMs || 5000);
  return body;
}

export function apiBody(form) {
  const checks = form.checks;
  const body = {
    url: form.url,
    authorization_confirmed: form.authorized,
    checks,
    expected_status_code: Number(form.expectedStatusCode),
    max_response_time_ms: Number(form.maxResponseTimeMs),
  };
  if (checks.includes("api_content_type")) body.expected_content_type = form.expectedContentType;
  const headers = headersObject(form.headers);
  if (Object.keys(headers).length) body.request_headers = headers;
  if (checks.includes("api_json_assertions")) body.json_assertions = cleanAssertions(form.assertions);
  if (checks.includes("api_json_schema")) body.response_schema = parseSchema(form.schemaText).schema;
  if (checks.includes("api_json_shape")) body.expected_json_example = parseSample(form.sampleText || "").sample;
  return body;
}

// Problems the user can fix before sending; the server still validates everything.
export function formProblems(testType, form) {
  const problems = [];
  const checks = form.checks;
  if (checks.length === 0) problems.push("Choose at least one check.");
  if (testType === "website") {
    if (checks.includes("text_present") && !form.expectedText.trim()) problems.push("Enter the expected text.");
    if (checks.includes("user_flow")) {
      const steps = form.flowSteps;
      if (steps.length === 0) problems.push("Add at least one flow step.");
      steps.forEach((step, index) => {
        const { needsSelector, needsValue } = flowAction(step.action);
        if (needsSelector && !step.selector.trim()) problems.push(`Step ${index + 1} needs a CSS selector.`);
        if (needsValue && !step.value) problems.push(`Step ${index + 1} needs a value.`);
      });
      if (steps.length && !steps.some((step) => ["expect_text", "expect_url"].includes(step.action))) {
        problems.push("A flow needs an Expect text or Expect URL step to decide pass or fail.");
      }
    }
  } else {
    if (checks.includes("api_json_assertions") && cleanAssertions(form.assertions).length === 0) problems.push("Add at least one JSON assertion.");
    if (checks.includes("api_json_schema")) {
      const { schema, error } = parseSchema(form.schemaText);
      if (error) problems.push(error);
      else if (!schema) problems.push("Paste the JSON Schema the response must match.");
    }
    if (checks.includes("api_json_shape")) {
      const { sample, error } = parseSample(form.sampleText || "");
      if (error) problems.push(error);
      else if (sample === undefined) problems.push("Paste a sample JSON response to compare shapes with.");
    }
  }
  return problems;
}

// Map Gemma's reviewed plan onto the form. Only fields the user can see and edit are filled.
export function planToForm(plan, allowedChecks) {
  const update = { checks: plan.checks.filter((name) => allowedChecks.includes(name)) };
  if (plan.suggested_expected_text) update.expectedText = plan.suggested_expected_text;
  if (plan.suggested_flow_steps?.length) {
    update.flowSteps = plan.suggested_flow_steps.map((step) => ({ action: step.action, selector: step.selector || "", value: step.value || "" }));
  }
  if (plan.suggested_json_assertions?.length) {
    update.assertions = plan.suggested_json_assertions.map((item) => ({
      path: item.path,
      operator: item.operator,
      expected: item.expected === null || item.expected === undefined ? "" : typeof item.expected === "string" ? item.expected : JSON.stringify(item.expected),
    }));
  }
  return update;
}

// Fill the API form from one imported OpenAPI operation.
export function operationToForm(operation) {
  const checks = ["api_status", "api_response_time"];
  const update = { url: operation.url || "", expectedStatusCode: String(operation.expected_status_code || 200) };
  if (operation.expected_content_type) {
    checks.push("api_content_type");
    update.expectedContentType = operation.expected_content_type;
  }
  if (operation.response_schema) {
    checks.push("api_json_schema");
    update.schemaText = JSON.stringify(operation.response_schema, null, 2);
  }
  update.checks = checks;
  return update;
}
