import { useEffect, useState } from "react";
import { apiJson } from "../api/client.js";
import {
  CHECK_LABELS, RECOVERY_LABELS, VERDICTS, groupChecks, investigationBody, normalizeOrderId, orderIdProblem,
} from "../lib/payment.js";
import { groupChecks as groupWebsiteChecks, label as websiteLabel } from "../lib/report.js";

const CLAIM_FIELDS = [["status", "Status"], ["total", "Total"], ["currency", "Currency"], ["capture_id", "Capture id"]];
const PAYPAL_FIELDS = [
  ["status", "Status"], ["amount", "Order amount"], ["capture_status", "Capture status"],
  ["capture_amount", "Captured amount"], ["capture_count", "Captures"], ["capture_id", "Capture id"],
];

function Record({ title, record, fields, empty }) {
  return <div className="record-card">
    <h3>{title}</h3>
    {record ? <dl className="payment-order">
      {fields.filter(([key]) => record[key] !== null && record[key] !== undefined).map(([key, text]) => <div key={key}>
        <dt>{text}</dt><dd>{String(record[key])}</dd>
      </div>)}
    </dl> : <p className="hint">{empty}</p>}
  </div>;
}

function Leaks({ leaks }) {
  return <div className="leak-list">
    {leaks.map((leak) => <div className="leak-card" key={leak.kind}>
      <div className="check-row-head">
        <strong>{leak.title}</strong>
        <span className="leak-amount">{leak.amount} {leak.currency}</span>
      </div>
      <p><b>Why:</b> {leak.root_cause}</p>
      <p><b>Recover:</b> {leak.recovery}</p>
      <button className="secondary-button" type="button" disabled title="Recovery actions need merchant approval and are not wired up yet.">
        {RECOVERY_LABELS[leak.recovery_action]} (needs approval)
      </button>
    </div>)}
  </div>;
}

function Explanation({ explanation }) {
  return <div className="explanation-card">
    <div className="check-row-head">
      <strong>What happened, in plain words</strong>
      <span className="count-chip">{explanation.source} · risk {explanation.risk_level}</span>
    </div>
    <p>{explanation.summary}</p>
    {explanation.next_steps.length > 0 && <ul>{explanation.next_steps.map((step) => <li key={step}>{step}</li>)}</ul>}
    {explanation.limitations.length > 0 && <small className="hint">{explanation.limitations.join(" ")}</small>}
  </div>;
}

function WebsiteScan({ report, onOpenReport }) {
  const counts = report.summary;
  return <div className="check-section">
    <div className="check-row-head">
      <h3>Website checks on the store</h3>
      <span className={`badge ${report.overall_status}`}>{report.overall_status}</span>
    </div>
    <p className="hint">{report.target_url} · passed {counts.passed}, failed {counts.failed}, review {counts.warnings}, errors {counts.errors}, skipped {counts.skipped}</p>
    {groupWebsiteChecks(report).filter((group) => group.checks.length).map((group) => <details className="website-group" key={group.key}>
      <summary><span>{group.title}</span><span className={`badge ${group.status}`}>{group.status}</span></summary>
      {group.checks.map((check) => <div className="check-row" key={check.check_id}>
        <div className="check-row-head">
          <strong>{websiteLabel(check.check_id)}</strong>
          <span className={`badge ${check.status}`}>{check.status}</span>
        </div>
        <small>{check.message}</small>
      </div>)}
    </details>)}
    {onOpenReport && <button className="secondary-button" type="button" onClick={() => onOpenReport(report)}>Open full website report</button>}
  </div>;
}

function Investigation({ result, onOpenReport }) {
  const [label, tone, line] = VERDICTS[result.verdict];
  return <div className="payment-result">
    <div className={`verdict-banner ${tone}`}>
      <div>
        <strong>{label}</strong>
        <small>Order <code>{result.order_id}</code> · {line} · {new Date(result.checked_at).toLocaleString()}</small>
      </div>
      <div className="verdict-money">
        <span>Money at risk</span>
        <b>{result.money_at_risk} {result.currency}</b>
      </div>
    </div>

    {result.leaks.length > 0 && <Leaks leaks={result.leaks} />}
    {result.explanation && <Explanation explanation={result.explanation} />}

    <div className="record-grid">
      <Record title="What the store says" record={result.store_claim} fields={CLAIM_FIELDS} empty="The store has no record of this order." />
      <Record title="What PayPal says" record={result.paypal_order} fields={PAYPAL_FIELDS} empty="PayPal has no record of this order." />
    </div>
    {result.expected_total && <p className="hint">Catalog price for this cart: <b>{result.expected_total} {result.currency}</b></p>}

    {groupChecks(result.checks).map(([title, checks]) => <div className="check-section" key={title}>
      <h3>{title}</h3>
      {checks.map((check) => <div className="check-row" key={check.name}>
        <div className="check-row-head">
          <strong>{CHECK_LABELS[check.name] || check.name}</strong>
          <span className={`badge ${check.status}`}>{check.status}</span>
        </div>
        <small>{check.message}</small>
      </div>)}
    </div>)}
    {result.website_report && <WebsiteScan report={result.website_report} onOpenReport={onOpenReport} />}
    <details className="raw-evidence"><summary>Raw evidence</summary><pre>{JSON.stringify(result.checks, null, 2)}</pre></details>
  </div>;
}

export default function PaymentPanel({ onOpenReport }) {
  const [config, setConfig] = useState(null);
  const [orderId, setOrderId] = useState("");
  const [probe, setProbe] = useState(true);
  const [website, setWebsite] = useState(true);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    apiJson("/api/paypal/status").then(setConfig).catch(() => setConfig(null));
  }, []);

  const problem = orderIdProblem(orderId);

  async function run(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await apiJson("/api/orders/investigate", { method: "POST", body: investigationBody(orderId, { probe, website }) }, "Investigation failed."));
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(false); }
  }

  return <section className="panel" id="payments" aria-labelledby="payments-heading">
    <div className="section-heading">
      <div><p className="eyebrow">PayRecover · PayPal Sandbox</p><h2 id="payments-heading">Investigate an order</h2></div>
      <span className={`status-pill ${config?.configured ? "online" : ""}`}><i />{config ? (config.configured ? "Sandbox connected" : "Sandbox keys missing") : "Sandbox status unknown"}</span>
    </div>
    <p className="hint">Paste an order id. PayRecover reads the store's claim, asks PayPal what really happened, probes the store for the hole behind any leak, scans the store's website, and explains it.</p>
    <form className="investigate-form" onSubmit={run}>
      <label className="field-label">Order id
        <input className="text-input" type="text" required placeholder="e.g. 5O190127TN364715T" value={orderId} onChange={(event) => setOrderId(event.target.value)} />
      </label>
      <button className="danger-button" type="submit" disabled={busy || !normalizeOrderId(orderId) || Boolean(problem) || config?.configured === false}>
        {busy ? <><span className="spinner" aria-hidden="true" /> Investigating…</> : "Run all checks"}
      </button>
    </form>
    <label className="scan-options probe-toggle">
      <input type="checkbox" checked={probe} onChange={(event) => setProbe(event.target.checked)} />
      Run store security probes (creates one unapproved Sandbox order; no money moves)
    </label>
    <label className="scan-options probe-toggle">
      <input type="checkbox" checked={website} onChange={(event) => setWebsite(event.target.checked)} />
      Run TestPilot website checks on the store (browser, security, accessibility, API contract; about a minute)
    </label>
    {problem && <ul className="problem-list"><li>{problem}</li></ul>}
    {config?.configured === false && <p className="notice notice-error" role="status">Set TESTPILOT_PAYPAL_CLIENT_ID and TESTPILOT_PAYPAL_CLIENT_SECRET in backend/.env, then restart the backend.</p>}
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {result && <Investigation result={result} onOpenReport={onOpenReport} />}
    <p className="hint">PayPal is only read, never charged. Payer details are never shown or stored. Gemma explains; the checks decide.</p>
  </section>;
}
