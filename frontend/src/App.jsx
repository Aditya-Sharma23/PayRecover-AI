import { useEffect, useState } from "react";
import { apiJson, getToken, setToken } from "./api/client.js";
import { AssertionEditor, FlowEditor, HeaderEditor } from "./components/Editors.jsx";
import HistoryPanel from "./components/HistoryPanel.jsx";
import OpenApiImport from "./components/OpenApiImport.jsx";
import ReportView from "./components/ReportView.jsx";
import { CATEGORIES } from "./lib/report.js";
import {
  API_CHECKS, FULL_SCAN, WEBSITE_CHECKS, apiBody, emptyAssertion, emptyStep, formProblems, operationToForm, planToForm, websiteBody,
} from "./lib/requestBody.js";

const DEFAULT_CHECKS = { website: FULL_SCAN, api: ["api_status", "api_response_time", "api_redirects"] };
const INITIAL_FORM = {
  url: "",
  authorized: false,
  checks: DEFAULT_CHECKS.website,
  expectedText: "",
  clickSelector: "",
  flowSteps: [emptyStep(), { action: "expect_text", selector: "", value: "" }],
  screenshotOnFailure: false,
  maxLoadTimeMs: "5000",
  expectedStatusCode: "200",
  expectedContentType: "application/json",
  maxResponseTimeMs: "5000",
  headers: [],
  assertions: [emptyAssertion()],
  schemaText: "",
  sampleText: "",
};
const TERMINAL = ["completed", "failed", "cancelled"];
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const METRICS = [
  ["total_runs", "Total runs", "▤", "Saved"],
  ["vulnerabilities", "Vulnerabilities", "⚠", "Potential"],
  ["security_issues", "Security issues", "⬡", "Review"],
  ["accessibility_issues", "Accessibility issues", "♿", "WCAG"],
];

function PlanReview({ plan, onApply, onDismiss }) {
  if (plan.error) {
    return <div className="notice notice-error" role="status"><strong>Gemma unavailable</strong><p>{plan.error}</p><p className="hint">Every check still runs without Gemma.</p></div>;
  }
  return <section className="ai-panel" aria-label="Gemma test plan">
    <p className="eyebrow">AI suggested plan · review before running</p>
    <p>{plan.rationale}</p>
    <strong>Checks</strong><p><code>{plan.checks.join(", ")}</code></p>
    <strong>Expectations</strong><ul>{plan.expectations.map((item) => <li key={item}>{item}</li>)}</ul>
    {plan.suggested_expected_text && <><strong>Suggested expected text</strong><p><code>{plan.suggested_expected_text}</code></p></>}
    {plan.suggested_flow_steps?.length > 0 && <><strong>Suggested flow (selectors are guesses until you check them)</strong><ol>{plan.suggested_flow_steps.map((step, index) => <li key={index}>{step.action} {step.selector && <code>{step.selector}</code>} {step.value}</li>)}</ol></>}
    {plan.suggested_json_assertions?.length > 0 && <><strong>Suggested JSON assertions</strong><ul>{plan.suggested_json_assertions.map((item, index) => <li key={index}><code>{item.path}</code> {item.operator} {item.expected !== null && item.expected !== undefined && <code>{JSON.stringify(item.expected)}</code>}</li>)}</ul></>}
    {plan.limitations.length > 0 && <><strong>Limitations</strong><ul>{plan.limitations.map((item) => <li key={item}>{item}</li>)}</ul></>}
    <div className="button-row">
      <button className="secondary-button" type="button" onClick={onApply}>Use this plan</button>
      <button className="secondary-button" type="button" onClick={onDismiss}>Dismiss</button>
    </div>
  </section>;
}

function CheckPicker({ available, checks, onToggle }) {
  return <div className="check-groups">
    {CATEGORIES.map(([key, title]) => {
      const items = available.filter(([, , category]) => category === key);
      if (!items.length) return null;
      return <fieldset className="check-group" key={key}>
        <legend>{title}</legend>
        {items.map(([name, text]) => <label className={`check-option ${checks.includes(name) ? "selected" : ""}`} key={name}>
          <input type="checkbox" checked={checks.includes(name)} onChange={() => onToggle(name)} />
          <span className="check-box" aria-hidden="true">✓</span>{text}
        </label>)}
      </fieldset>;
    })}
  </div>;
}

export default function App() {
  const [serviceState, setServiceState] = useState("Connecting");
  const [gemmaState, setGemmaState] = useState("Gemma status unknown");
  const [testType, setTestType] = useState("website");
  const [mode, setMode] = useState("full");
  const [form, setForm] = useState(INITIAL_FORM);
  const [background, setBackground] = useState(false);
  const [result, setResult] = useState(null);
  const [status, setStatus] = useState("Paste a URL and start a scan. Only test sites you own or have permission to assess.");
  const [historyKey, setHistoryKey] = useState(0);
  const [goal, setGoal] = useState("");
  const [aiPlan, setAiPlan] = useState(null);
  const [busy, setBusy] = useState(null);
  const [token, setTokenState] = useState(getToken());
  const [needsToken, setNeedsToken] = useState(false);
  const [stats, setStats] = useState(null);

  useEffect(() => {
    apiJson("/api/health")
      .then((data) => setServiceState(data.status === "ok" ? "API connected" : "API unavailable"))
      .catch(() => setServiceState("API offline"));
    apiJson("/api/ai/status")
      .then((data) => setGemmaState(data.configured ? `Gemma ready (${data.model})` : "Gemma off · built-in summaries"))
      .catch(() => setGemmaState("Gemma status unavailable"));
    apiJson("/api/me")
      .then(() => setNeedsToken(false))
      .catch((error) => setNeedsToken(/authentication required/.test(error.message)));
  }, [token]);

  useEffect(() => {
    apiJson("/api/dashboard").then(setStats).catch(() => setStats(null));
  }, [historyKey, token]);

  const available = testType === "website" ? WEBSITE_CHECKS : API_CHECKS;
  const fullScan = testType === "website" && mode === "full";
  const activeProbes = form.checks.includes("active_probes");
  const checks = fullScan ? [...FULL_SCAN, ...(activeProbes ? ["active_probes"] : [])] : form.checks;
  const effective = { ...form, checks };
  const problems = formProblems(testType, effective);
  const set = (change) => setForm((current) => ({ ...current, ...change }));
  const switchTestType = (type) => {
    setTestType(type);
    setMode(type === "website" ? "full" : "custom");
    set({ checks: DEFAULT_CHECKS[type] });
    setAiPlan(null);
    setResult(null);
  };
  const toggleCheck = (name) => set({ checks: form.checks.includes(name) ? form.checks.filter((item) => item !== name) : [...form.checks, name] });
  const toggleActive = (on) => set({ checks: on ? [...form.checks.filter((item) => item !== "active_probes"), "active_probes"] : form.checks.filter((item) => item !== "active_probes") });

  async function runInBackground(body) {
    const job = await apiJson("/api/jobs", { method: "POST", body: { job_type: testType, [testType]: body } }, "The job could not be queued.");
    setBusy(`Queued as job ${job.job_id.slice(0, 8)}…`);
    setHistoryKey((value) => value + 1);
    let record = job;
    for (let attempt = 0; attempt < 300 && !TERMINAL.includes(record.status); attempt += 1) {
      await pause(1000);
      record = await apiJson(`/api/jobs/${job.job_id}`);
      setBusy(`Background job ${record.status}…`);
    }
    if (record.status !== "completed") throw new Error(`The background job ${record.status}${record.error ? ` (${record.error})` : ""}.`);
    return apiJson(`/api/runs/${record.report_id}`);
  }

  async function submit(event) {
    event.preventDefault();
    setResult(null);
    try {
      setBusy("Checking destination…");
      await apiJson("/api/targets/preflight", { method: "POST", body: { url: form.url, authorization_confirmed: form.authorized } }, "Preflight could not be completed.");
      setBusy(`Running ${checks.length} check(s)…`);
      setStatus("Preflight passed: URL and every DNS answer are public. Running checks; browser and contract checks can take up to a minute.");
      const body = testType === "website" ? websiteBody(effective) : apiBody(effective);
      const report = background
        ? await runInBackground(body)
        : await apiJson(`/api/tests/${testType}`, { method: "POST", body }, "The run could not be completed.");
      setResult(report);
      setStatus(`Run ${report.overall_status}: ${report.summary.passed} passed, ${report.summary.failed} failed, ${report.summary.warnings} warnings, ${report.summary.errors} errors, ${report.summary.skipped} skipped.`);
      setHistoryKey((value) => value + 1);
      setTimeout(() => document.getElementById("report")?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    } catch (error) {
      setStatus(error.message);
    } finally { setBusy(null); }
  }

  async function generatePlan() {
    setBusy("Gemma is drafting a plan…");
    setAiPlan(null);
    try {
      setAiPlan(await apiJson("/api/ai/plan", {
        method: "POST",
        body: { goal, target_url: form.url || null, test_type: testType, authorized: form.authorized },
      }, "Gemma could not create a plan."));
    } catch (error) {
      setAiPlan({ error: error.message });
    } finally { setBusy(null); }
  }

  function applyPlan() {
    set(planToForm(aiPlan, available.map(([key]) => key)));
    setMode("custom");
    setAiPlan(null);
  }

  function saveToken(value) {
    setToken(value.trim());
    setTokenState(value.trim());
    setHistoryKey((current) => current + 1);
  }

  const online = serviceState === "API connected";
  return <div className="app-shell">
    <aside className="sidebar" aria-label="Main navigation">
      <a className="brand" href="#dashboard"><span className="brand-mark" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M3 22.5 45 3 29 45l-7-15.2L3 22.5Zm19.2 7.3L45 3 18 25.2" /></svg></span><span>Test<span className="brand-accent">Pilot</span> AI</span></a>
      <nav className="side-nav">
        <a href="#dashboard"><span aria-hidden="true">⌂</span> Dashboard</a>
        <a href="#scan"><span aria-hidden="true">⌕</span> New scan</a>
        <a href="#report"><span aria-hidden="true">◉</span> Latest report</a>
        <a href="#findings"><span aria-hidden="true">⚠</span> Vulnerabilities</a>
        <a href="#history"><span aria-hidden="true">▤</span> Run history</a>
      </nav>
      <div className="sidebar-note">
        <span className="shield" aria-hidden="true">⬡</span>
        <strong>Evidence first</strong>
        <p>Real browser and HTTP runners decide every result. Gemma explains; it never changes a verdict.</p>
      </div>
    </aside>
    <main className="main-content" id="dashboard">
      <section className="welcome">
        <div><p className="eyebrow">TestPilot AI · final build</p><h1>Testing &amp; Security Overview</h1><p>Website, API, security and accessibility checks in one safe runner.</p></div>
        <div className="date-label">
          <span className={`status-pill ${online ? "online" : ""}`}><i />{serviceState}</span>
          <span className="status-pill"><i className="gemma" />{gemmaState}</span>
        </div>
      </section>

      <section className="metrics" aria-label="Run statistics">
        {METRICS.map(([key, title, icon, trend]) => <article className="metric-card" key={key}>
          <span className="metric-icon" aria-hidden="true">{icon}</span>
          <div><p>{title}</p><strong>{stats ? stats[key] : "–"}</strong></div>
          <span className="metric-trend">{trend}</span>
        </article>)}
      </section>

      <section className="scan-panel panel" id="scan" aria-labelledby="scan-title">
        <div className="tabs" role="tablist" aria-label="Test type">
          <button type="button" role="tab" aria-selected={testType === "website"} className={testType === "website" ? "selected" : ""} onClick={() => switchTestType("website")}>Website scan</button>
          <button type="button" role="tab" aria-selected={testType === "api"} className={testType === "api" ? "selected" : ""} onClick={() => switchTestType("api")}>API test</button>
        </div>
        <h2 id="scan-title" className="sr-only">{testType === "website" ? "Website scan" : "API test"}</h2>
        <form onSubmit={submit}>
          <div className="scan-bar">
            <label className="scan-url"><span aria-hidden="true">↗</span><span className="sr-only">{testType === "website" ? "Website URL to scan" : "API endpoint URL"}</span>
              <input type="url" required placeholder={testType === "website" ? "Enter a website URL (e.g. https://example.com)" : "Enter an API endpoint (e.g. https://api.example.com/health)"} value={form.url} onChange={(event) => set({ url: event.target.value })} />
            </label>
            <button className="danger-button" type="submit" disabled={!form.authorized || !form.url || Boolean(busy) || problems.length > 0}>
              {busy ? <><span className="spinner" aria-hidden="true" /> {busy}</> : <><span aria-hidden="true">▷</span> Start scan</>}
            </button>
          </div>

          <div className="scan-options">
            {testType === "website" && <div className="mode-switch" role="radiogroup" aria-label="Scan mode">
              <label className={mode === "full" ? "selected" : ""}><input type="radio" name="mode" checked={mode === "full"} onChange={() => setMode("full")} /> Full scan ({FULL_SCAN.length} checks)</label>
              <label className={mode === "custom" ? "selected" : ""}><input type="radio" name="mode" checked={mode === "custom"} onChange={() => setMode("custom")} /> Custom checks</label>
            </div>}
            <label className="consent-label"><input type="checkbox" checked={form.authorized} onChange={(event) => set({ authorized: event.target.checked })} /> I own this site or have permission to test it</label>
            {testType === "website" && <label><input type="checkbox" checked={activeProbes} onChange={(event) => toggleActive(event.target.checked)} /> Run inert XSS and access-control probes</label>}
            {testType === "website" && <label><input type="checkbox" checked={form.screenshotOnFailure} onChange={(event) => set({ screenshotOnFailure: event.target.checked })} /> Screenshot on failure</label>}
            <label><input type="checkbox" checked={background} onChange={(event) => setBackground(event.target.checked)} /> Run as background job</label>
          </div>

          {(testType === "api" || mode === "custom") && <CheckPicker available={available} checks={form.checks} onToggle={toggleCheck} />}

          <details className="advanced-options" open={checks.some((name) => ["text_present", "user_flow", "api_json_assertions", "api_json_schema", "api_json_shape"].includes(name)) || undefined}>
            <summary>Inputs, Gemma planning and advanced options</summary>
            <div className="advanced-grid">
              <div>
                <label className="field-label" htmlFor="test-goal">Testing goal for Gemma</label>
                <input id="test-goal" className="text-input" type="text" placeholder="e.g. Signing in without a password shows Password is required" value={goal} onChange={(event) => setGoal(event.target.value)} />
                <button className="secondary-button" type="button" onClick={generatePlan} disabled={!form.authorized || !goal || Boolean(busy)}>Ask Gemma to structure the checks</button>
                {!form.authorized && goal && <p className="hint">Confirm permission to ask Gemma for a plan.</p>}
                {aiPlan && <PlanReview plan={aiPlan} onApply={applyPlan} onDismiss={() => setAiPlan(null)} />}
                {testType === "api" && <OpenApiImport onUse={(operation) => set(operationToForm(operation))} />}
              </div>
              <div className="field-stack">
                {testType === "website" && <>
                  <label className="field-label" htmlFor="max-load">Load time limit (ms)<input id="max-load" className="text-input" type="number" min="100" max="60000" value={form.maxLoadTimeMs} onChange={(event) => set({ maxLoadTimeMs: event.target.value })} /></label>
                  {checks.includes("text_present") && <>
                    <label className="field-label" htmlFor="expected-text">Expected visible text (exact, case-sensitive)<input id="expected-text" className="text-input" type="text" maxLength={500} placeholder="e.g. Email is required" value={form.expectedText} onChange={(event) => set({ expectedText: event.target.value })} /></label>
                    <label className="field-label" htmlFor="click-selector">Optional: click this element first (CSS selector)<input id="click-selector" className="text-input" type="text" maxLength={200} placeholder="e.g. #sign-in-button" value={form.clickSelector} onChange={(event) => set({ clickSelector: event.target.value })} /></label>
                  </>}
                </>}
                {testType === "api" && <>
                  <label className="field-label" htmlFor="expected-status">Expected status<input id="expected-status" className="text-input" type="number" min="100" max="599" value={form.expectedStatusCode} onChange={(event) => set({ expectedStatusCode: event.target.value })} /></label>
                  <label className="field-label" htmlFor="max-response-time">Max response time (ms)<input id="max-response-time" className="text-input" type="number" min="1" max="60000" value={form.maxResponseTimeMs} onChange={(event) => set({ maxResponseTimeMs: event.target.value })} /></label>
                  {checks.includes("api_content_type") && <label className="field-label" htmlFor="expected-content-type">Expected content type<input id="expected-content-type" className="text-input" type="text" value={form.expectedContentType} onChange={(event) => set({ expectedContentType: event.target.value })} /></label>}
                </>}
              </div>
            </div>
            {testType === "website" && checks.includes("user_flow") && <FlowEditor steps={form.flowSteps} onChange={(flowSteps) => set({ flowSteps })} />}
            {testType === "api" && checks.includes("api_json_assertions") && <AssertionEditor rows={form.assertions} onChange={(assertions) => set({ assertions })} />}
            {testType === "api" && checks.includes("api_json_schema") && <label className="field-label editor" htmlFor="response-schema">JSON Schema the response must match (draft 2020-12)<textarea id="response-schema" rows={6} placeholder='{"type": "object", "required": ["id"]}' value={form.schemaText} onChange={(event) => set({ schemaText: event.target.value })} /></label>}
            {testType === "api" && checks.includes("api_json_shape") && <label className="field-label editor" htmlFor="sample-json">Sample JSON response (keys and types are compared)<textarea id="sample-json" rows={5} placeholder='{"id": 1, "tags": ["a"]}' value={form.sampleText} onChange={(event) => set({ sampleText: event.target.value })} /></label>}
            {testType === "api" && <HeaderEditor rows={form.headers} onChange={(headers) => set({ headers })} />}
            <details className="token-settings" open={needsToken || undefined}>
              <summary>{needsToken ? "This server needs an API token · paste it here" : "API token (only when backend authentication is enabled)"}</summary>
              <input type="password" className="text-input" autoComplete="off" placeholder="Paste your TestPilot API token" defaultValue={token} onBlur={(event) => saveToken(event.target.value)} />
              <p className="hint">Kept in this browser tab's session storage only.</p>
            </details>
          </details>
          {problems.length > 0 && form.url && <ul className="problem-list">{problems.map((problem) => <li key={problem}>{problem}</li>)}</ul>}
        </form>
        <p className="hint scan-status" role="status" aria-live="polite">{status}</p>
        <p className="hint">{testType === "website"
          ? "Every browser request goes through the safe runner: same-origin GET/HEAD only, pinned to checked public IPs, no form submissions, no WebSockets."
          : "One bounded GET is sent. Redirects, request bodies and non-public destinations are blocked. Header values are never stored."}</p>
      </section>

      {result ? <ReportView key={result.run_id} report={result} /> : <section className="panel" id="report"><div className="section-heading"><div><p className="eyebrow">Latest report</p><h2>No report yet</h2></div></div><p className="empty-state">Run a scan, or open one from Run history, to see results grouped by HTTP response, content, security, accessibility and API contract.</p></section>}

      <HistoryPanel refreshKey={`${historyKey}-${token}`} onOpen={(report) => { setResult(report); setTimeout(() => document.getElementById("report")?.scrollIntoView({ behavior: "smooth" }), 50); }} />
      <footer>TestPilot AI · HTTP · Playwright + axe · security probes · Schemathesis · Gemma 4</footer>
    </main>
  </div>;
}
