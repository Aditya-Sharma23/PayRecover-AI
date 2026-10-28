import { useEffect, useState } from "react";
import { apiBlobUrl, apiJson, openHtmlPage } from "../api/client.js";
import { collectFindings, groupChecks, label } from "../lib/report.js";

const STATUS_TEXT = { passed: "Passed", failed: "Failed", warning: "Review", skipped: "Skipped", error: "Error", not_run: "Not run", blocked: "Blocked", inconclusive: "Inconclusive" };

function Badge({ status }) {
  return <span className={`badge ${status}`}>{STATUS_TEXT[status] || status}</span>;
}

function downloadReport(report) {
  const objectUrl = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = `testpilot-${report.run_id}.json`;
  link.click();
  URL.revokeObjectURL(objectUrl);
}

function Screenshot({ artifactId }) {
  const [src, setSrc] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let objectUrl = null;
    apiBlobUrl(`/api/artifacts/${artifactId}`)
      .then((url) => { objectUrl = url; setSrc(url); })
      .catch((reason) => setError(reason.message));
    return () => { if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [artifactId]);
  if (error) return <p className="hint">{error}</p>;
  if (!src) return <p className="hint">Loading failure screenshot…</p>;
  return <a href={src} target="_blank" rel="noreferrer"><img className="evidence-shot" src={src} alt="Viewport screenshot captured when this check failed" /></a>;
}

function FlowSteps({ steps }) {
  return <ol className="evidence-steps">{steps.map((step) => <li key={step.step} className={`step-${step.status}`}>
    <span className="step-status">{step.status}</span>
    <span><strong>{step.action}</strong>{step.selector && <code> {step.selector}</code>}{step.value && <> “{step.value}”</>}{step.value_length !== undefined && <> ({step.value_length} characters typed)</>}
      {step.reason && <small>{step.reason}</small>}</span>
  </li>)}</ol>;
}

function Assertions({ assertions }) {
  return <ul className="evidence-list">{assertions.map((item, index) => <li key={index}>
    <span className={`impact ${item.passed ? "impact-pass" : "impact-serious"}`}>{item.passed ? "held" : "broke"}</span>{" "}
    <code>{item.path}</code> {item.operator.replace("_", " ")} {item.expected !== null && <code>{JSON.stringify(item.expected)}</code>}
    <small> observed: {item.found ? <code>{JSON.stringify(item.actual)}</code> : "missing"}</small>
  </li>)}</ul>;
}

function Evidence({ check }) {
  const evidence = check.evidence || {};
  return <>
    {Array.isArray(evidence.steps) && <FlowSteps steps={evidence.steps} />}
    {Array.isArray(evidence.assertions) && <Assertions assertions={evidence.assertions} />}
    {Array.isArray(evidence.errors) && check.check_id === "api_json_schema" && <ul className="evidence-list">{evidence.errors.map((item, index) => <li key={index}><code>{item.path}</code> {item.message}</li>)}</ul>}
    {check.limitations?.length > 0 && <small className="limitations">{check.limitations.join(" ")}</small>}
    {evidence.screenshot_artifact_id && <Screenshot artifactId={evidence.screenshot_artifact_id} />}
    <details className="raw-evidence"><summary>Raw evidence</summary><pre>{JSON.stringify(evidence, null, 1)}</pre></details>
  </>;
}

function CheckRow({ check, detailed }) {
  return <article className="check-row">
    <div className="check-row-head"><div><strong>{label(check.check_id)}</strong><small>{check.message}</small></div><Badge status={check.status} /></div>
    {detailed && <Evidence check={check} />}
  </article>;
}

function Assessment({ report }) {
  const [explanation, setExplanation] = useState(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let current = true;
    apiJson("/api/ai/explain", { method: "POST", body: { report } }, "The report could not be summarized.")
      .then((value) => { if (current) setExplanation(value); })
      .catch((error) => { if (current) setExplanation({ error: error.message }); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [report, attempt]);
  const explain = () => { setLoading(true); setAttempt((value) => value + 1); };
  const risk = (explanation?.risk_level || "UNKNOWN").toLowerCase();
  return <section className="ai-overview" aria-label="AI assessment">
    <div className="section-heading"><h3>{explanation?.source === "Gemma" ? "Gemma assessment" : "Assessment"}</h3>
      {explanation && !explanation.error && <span className={`risk-badge risk-${risk}`}>{explanation.risk_level} RISK</span>}</div>
    {loading && <p className="hint"><span className="spinner" aria-hidden="true" /> Reading the evidence…</p>}
    {!loading && explanation?.error && <p className="hint">{explanation.error}</p>}
    {!loading && explanation && !explanation.error && <>
      <p className="ai-executive">{explanation.summary}</p>
      {explanation.findings.length > 0 && <><h4>Key points</h4><ul className="ai-list">{explanation.findings.map((item) => <li key={item}>{item}</li>)}</ul></>}
      {explanation.next_steps.length > 0 && <><h4>Recommended next steps</h4><ol className="ai-list">{explanation.next_steps.map((item) => <li key={item}>{item}</li>)}</ol></>}
      {explanation.limitations.length > 0 && <><h4>Coverage notes</h4><ul className="ai-list muted">{explanation.limitations.map((item) => <li key={item}>{item}</li>)}</ul></>}
      <small className="hint">Source: {explanation.source}. Advisory only: the statuses above come from the runners.</small>
    </>}
    <div className="button-row"><button className="secondary-button" type="button" disabled={loading} onClick={explain}>Summarize again</button></div>
  </section>;
}

export default function ReportView({ report }) {
  const [linkError, setLinkError] = useState(null);
  const counts = report.summary;
  const groups = groupChecks(report).filter((group) => group.checks.length);
  const findings = collectFindings(report);

  async function openHtml() {
    setLinkError(null);
    try { await openHtmlPage(`/api/runs/${report.run_id}/html`); } catch (error) { setLinkError(error.message); }
  }

  return <section className="report" id="report" aria-label="Run report">
    <div className="panel result-heading">
      <div>
        <p className="eyebrow">Latest report · {new Date(report.finished_at).toLocaleString()}</p>
        <h2>{report.target_url}</h2>
        <p className="hint">{report.checks.length} checks · passed {counts.passed}, failed {counts.failed}, review {counts.warnings}, errors {counts.errors}, skipped {counts.skipped}</p>
      </div>
      <div className="result-actions">
        <Badge status={report.overall_status} />
        <button className="secondary-button" type="button" onClick={() => downloadReport(report)}>JSON</button>
        <button className="secondary-button" type="button" onClick={openHtml}>HTML report</button>
      </div>
    </div>
    {linkError && <p className="notice notice-error">{linkError}</p>}

    <div className="reports-grid">
      {groups.map((group) => <section className="panel report-panel" key={group.key}>
        <div className="section-heading"><div><p className="eyebrow">{group.description}</p><h2>{group.title}</h2></div><Badge status={group.status} /></div>
        {group.checks.map((check) => <CheckRow key={check.check_id} check={check} />)}
      </section>)}
    </div>

    <section className="panel" id="findings" aria-labelledby="findings-title">
      <div className="section-heading"><div><p className="eyebrow">Security and accessibility findings</p><h2 id="findings-title">Vulnerabilities found <span className="count-chip">{findings.length} item{findings.length === 1 ? "" : "s"}</span></h2></div></div>
      <p className="hint">Automated findings are leads to review, not proof of exploitability.</p>
      {findings.length ? <div className="vulnerability-grid">{findings.map((item, index) => <article className="vulnerability-card" key={`${item.title}-${index}`}>
        <div className="vulnerability-card-heading"><span className="vulnerability-source">{item.source}</span><span className={`severity ${String(item.severity).toLowerCase()}`}>{item.severity}</span></div>
        <h3>{item.title}</h3>
        <p>{item.detail}</p>
        <p className="vulnerability-fix"><strong>Suggested fix</strong><br />{String(item.fix).startsWith("https://") ? <a href={item.fix} target="_blank" rel="noreferrer">{item.fix}</a> : item.fix}</p>
      </article>)}</div> : <p className="empty-state">No security findings or accessibility violations were reported by the selected checks.</p>}
    </section>

    <section className="panel">
      <Assessment report={report} />
      <details className="quality-details">
        <summary>All {report.checks.length} check results with evidence</summary>
        <div className="check-detail-list">{report.checks.map((check) => <CheckRow key={check.check_id} check={check} detailed />)}</div>
      </details>
      {report.warnings?.length > 0 && <p className="hint">{report.warnings.join(" ")}</p>}
    </section>
  </section>;
}
