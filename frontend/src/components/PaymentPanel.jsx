import { useEffect, useState } from "react";
import { apiJson, openHtmlPage } from "../api/client.js";
import {
  CHECK_LABELS, RECOVERY_LABELS, RECOVERY_STATUS, STORES, URGENCY, VERDICTS, XSS_OUTCOMES, XSS_VERDICTS, approvalQuestion,
  evidencePath, waitingText,
  groupChecks, investigationBody, recoveryBody,
  normalizeOrderId, orderIdProblem, sortPayloads,
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

function RecoveryOutcome({ record }) {
  const [label, tone] = RECOVERY_STATUS[record.status] || RECOVERY_STATUS.failed;
  return <div className="recovery-outcome">
    <div className="check-row-head"><strong>{record.message}</strong><span className={`badge ${tone}`}>{label}</span></div>
    {record.verification.map((check) => <small key={check.name + check.message}>{check.status === "passed" ? "✓" : "✗"} {check.message}</small>)}
  </div>;
}

export function WaitingClock({ since }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(timer);
  }, []);
  return <span className="waiting-clock">⏱ {waitingText(since, now)}</span>;
}

export function EvidenceButton({ orderId, store }) {
  const [error, setError] = useState(null);
  async function open() {
    setError(null);
    try { await openHtmlPage(evidencePath(orderId, store)); } catch (reason) { setError(reason.message); }
  }
  return <>
    <button className="secondary-button" type="button" onClick={open} title="A printable page for a PayPal dispute or chargeback">Evidence pack</button>
    {error && <small className="notice-error">{error}</small>}
  </>;
}

function LeakCard({ leak, orderId, store, onRecovered }) {
  const [busy, setBusy] = useState(false);
  const [record, setRecord] = useState(null);
  const [error, setError] = useState(null);

  async function approve() {
    if (!window.confirm(approvalQuestion(leak, orderId))) return;
    setBusy(true);
    setError(null);
    try {
      const done = await apiJson("/api/recoveries", { method: "POST", body: recoveryBody(orderId, store, leak.kind) }, "The recovery could not be completed.");
      setRecord(done);
      onRecovered?.(done);
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(false); }
  }

  return <div className="leak-card">
    <div className="check-row-head">
      <strong>{leak.title}</strong>
      <span className="leak-amount">{leak.amount} {leak.currency}</span>
    </div>
    <p className="leak-urgency"><span className={`badge ${URGENCY[leak.urgency]?.[1] || "skipped"}`}>{URGENCY[leak.urgency]?.[0] || leak.urgency}</span>
      {leak.waiting_since && <WaitingClock since={leak.waiting_since} />}</p>
    <p><b>Why:</b> {leak.root_cause}</p>
    <p><b>Recover:</b> {leak.recovery}</p>
    {!record && !leak.targets?.length && <p className="notice" role="note">No PayPal action is possible for this leak, so there is nothing to approve: hold the shipment.</p>}
    {!record && leak.targets?.length > 0 && <button className="danger-button approve-button" type="button" disabled={busy} onClick={approve}>
      {busy ? <><span className="spinner" aria-hidden="true" /> Asking PayPal…</> : `Approve: ${RECOVERY_LABELS[leak.recovery_action]} ${leak.amount} ${leak.currency}`}
    </button>}
    {record && <RecoveryOutcome record={record} />}
    {error && <p className="notice notice-error" role="alert">{error}</p>}
  </div>;
}

function Leaks({ leaks, orderId, store, onRecovered }) {
  return <div className="leak-list">
    {leaks.map((leak) => <LeakCard key={leak.kind} leak={leak} orderId={orderId} store={store} onRecovered={onRecovered} />)}
    <small className="hint">Nothing moves until you approve. PayRecover then acts in PayPal Sandbox once and checks PayPal again to confirm it worked.</small>
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

function XssScan({ scan }) {
  const [label, tone] = XSS_VERDICTS[scan.status] || XSS_VERDICTS.error;
  return <div className="check-section xss-section">
    <div className="check-row-head">
      <h3>XSS check (cross-site scripting)</h3>
      <span className={`badge ${tone}`}>{label}</span>
    </div>
    <p className="hint">{scan.summary} {scan.payload_count} payloads · {scan.parameters.length} parameters + URL path · {scan.requests} requests · Content-Security-Policy {scan.content_security_policy ? "present" : "missing"}.</p>
    {scan.findings.map((item) => <div className="leak-card" key={item.title}>
      <div className="check-row-head"><strong>{item.title}</strong><span className="leak-amount">{item.severity}</span></div>
      <p>{item.detail}</p>
      <p><b>Fix:</b> {item.recommendation}</p>
    </div>)}
    {scan.payloads.length > 0 && <details className="website-group" open={scan.status === "failed"}>
      <summary><span>All {scan.payloads.length} payloads</span><span className={`badge ${tone}`}>{label}</span></summary>
      <table className="xss-table">
        <thead><tr><th>Payload</th><th>Context</th><th>Result</th><th>Where</th></tr></thead>
        <tbody>{sortPayloads(scan.payloads).map((item) => {
          const [text, cls] = XSS_OUTCOMES[item.outcome] || XSS_OUTCOMES.error;
          const where = item.reflected_in.length ? item.reflected_in : item.encoded_in || [];
          return <tr key={item.id}>
            <td><strong>{item.label}</strong><code>{item.payload}</code></td>
            <td>{item.context}</td>
            <td><span className={`badge ${cls}`}>{text}</span></td>
            <td>{where.length ? where.slice(0, 4).join(", ") + (where.length > 4 ? ` +${where.length - 4}` : "") : "—"}</td>
          </tr>;
        })}</tbody>
      </table>
    </details>}
    {scan.limitations?.length > 0 && <small className="hint">{scan.limitations.join(" ")}</small>}
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

function Investigation({ result, onOpenReport, onRecovered }) {
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
        <EvidenceButton orderId={result.order_id} store={result.store} />
      </div>
    </div>

    {result.leaks.length > 0 && <Leaks leaks={result.leaks} orderId={result.order_id} store={result.store} onRecovered={onRecovered} />}
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
    {result.xss_scan && <XssScan scan={result.xss_scan} />}
    {result.website_report && <WebsiteScan report={result.website_report} onOpenReport={onOpenReport} />}
    <details className="raw-evidence"><summary>Raw evidence</summary><pre>{JSON.stringify(result.checks, null, 2)}</pre></details>
  </div>;
}

export default function PaymentPanel({ onOpenReport, onWebsiteReport, onRecovered, embedded = false }) {
  const [config, setConfig] = useState(null);
  const [orderId, setOrderId] = useState("");
  const [probe, setProbe] = useState(true);
  const [website, setWebsite] = useState(true);
  const [xss, setXss] = useState(true);
  const [store, setStore] = useState("main");
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
      const investigation = await apiJson("/api/orders/investigate", { method: "POST", body: investigationBody(orderId, { store, probe, website, xss }) }, "Investigation failed.");
      setResult(investigation);
      // The store's website scan is a normal run: show it as the latest report and refresh history.
      if (investigation.website_report && onWebsiteReport) onWebsiteReport(investigation.website_report);
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(false); }
  }

  const Wrapper = embedded ? "div" : "section";
  return <Wrapper className={embedded ? "embedded-panel" : "panel"} id="payments" aria-labelledby="payments-heading">
    <div className="section-heading">
      <div>{!embedded && <p className="eyebrow">PayRecover · PayPal Sandbox</p>}<h2 id="payments-heading">{embedded ? "Check one order in depth" : "Investigate an order"}</h2></div>
      <span className={`status-pill ${config?.configured ? "online" : ""}`}><i />{config ? (config.configured ? "Sandbox connected" : "Sandbox keys missing") : "Sandbox status unknown"}</span>
    </div>
    <p className="hint">Paste an order id. PayRecover reads the store's claim, asks PayPal what really happened, probes the store for the hole behind any leak, scans the store's website, checks it for XSS, and explains it.</p>
    <form className="investigate-form" onSubmit={run}>
      <label className="field-label">Store
        <select className="text-input" value={store} onChange={(event) => setStore(event.target.value)}>
          {STORES.map(([key, text]) => <option key={key} value={key}>{text}</option>)}
        </select>
      </label>
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
      Run the website checks on the store (browser, security, accessibility, API contract; about a minute)
    </label>
    <label className="scan-options probe-toggle">
      <input type="checkbox" checked={xss} onChange={(event) => setXss(event.target.checked)} />
      Run the XSS check on the store (15 inert payloads in 15 parameters and the URL path; nothing is executed)
    </label>
    {problem && <ul className="problem-list"><li>{problem}</li></ul>}
    {config?.configured === false && <p className="notice notice-error" role="status">Set TESTPILOT_PAYPAL_CLIENT_ID and TESTPILOT_PAYPAL_CLIENT_SECRET in backend/.env, then restart the backend.</p>}
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {result && <Investigation result={result} onOpenReport={onOpenReport} onRecovered={onRecovered} />}
    <p className="hint">Investigating only reads PayPal; money moves only when you approve a recovery. Payer details are never shown or stored. The AI explains; the checks decide.</p>
  </Wrapper>;
}
