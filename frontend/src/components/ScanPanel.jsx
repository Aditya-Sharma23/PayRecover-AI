import { useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import { apiJson } from "../api/client.js";
import { gridTheme } from "../lib/grid.js";
import {
  RECOVERY_LABELS, RECOVERY_STATUS, STORES, URGENCY, approvalQuestion, recoveryBody, remainingAtRisk, scanBody, scanRows,
} from "../lib/payment.js";
import { SCAN_VERDICTS, withSampleHint } from "../lib/assist.js";
import MarketPrices from "./MarketPrices.jsx";
import { EvidenceButton, ExpiresClock, WaitingClock } from "./PaymentPanel.jsx";

function UrgencyCell({ value }) {
  const [label, tone] = URGENCY[value] || URGENCY.normal;
  return <span className={`badge ${tone}`}>{label}</span>;
}

function WaitingCell({ value, data }) {
  if (data.expires_at) return <ExpiresClock until={data.expires_at} />;
  return value ? <WaitingClock since={value} /> : <span className="hint">—</span>;
}

function ActionsCell({ data, context }) {
  const outcome = context.outcomes[data.id];
  if (outcome?.record) {
    const [label, tone] = RECOVERY_STATUS[outcome.record.status] || RECOVERY_STATUS.failed;
    return <div className="grid-actions"><span className={`badge ${tone}`} title={outcome.record.message}>{label}</span>
      <EvidenceButton orderId={data.order_id} store={context.store} /></div>;
  }
  if (!data.leak.targets?.length) {
    return <div className="grid-actions"><span className="badge warning" title={data.leak.recovery}>Hold shipment</span>
      <EvidenceButton orderId={data.order_id} store={context.store} /></div>;
  }
  return <div className="grid-actions">
    <button className="danger-button" type="button" disabled={outcome?.busy}
      title={outcome?.error || ""} onClick={() => context.approve(data)}>
      {outcome?.busy ? "Asking PayPal…" : `Approve ${RECOVERY_LABELS[data.action]?.toLowerCase()}`}
    </button>
    <EvidenceButton orderId={data.order_id} store={context.store} />
    {outcome?.error && <span className="badge failed" title={outcome.error}>Error</span>}
  </div>;
}

// The money result only: what leaked, or why the store is not called all clear. Security has its own card.
function Verdict({ result, plan }) {
  const [title, tone, text] = SCAN_VERDICTS[result.verdict] || SCAN_VERDICTS.clear;
  return <div className={`scan-verdict ${tone}`} role="status">
    <strong>{title}</strong>
    <p>{result.verdict === "leaking" ? plan.summary : text}</p>
    {result.waiting_reasons?.length > 0 && <ul className="waiting-reasons">{result.waiting_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
    {result.verdict === "leaking" && <small className="hint">Plan by {plan.source}{plan.notes.length > 0 ? ` · ${plan.notes.join(" ")}` : ""}</small>}
  </div>;
}

// The store-wide security check, apart from the money: holes that could let money leak later.
function StoreSecurity({ security, open }) {
  if (!security) return null;
  const findings = security.findings || [];
  if (!security.checked) return <p className="hint">Security check incomplete: {security.note}</p>;
  if (!findings.length) return <p className="security-clear"><span className="badge passed">Store security</span> No holes found: prices are fixed on the server, no XSS, security headers sent.</p>;
  return <details className="store-security" open={open}>
    <summary><span className="badge warning">Store security</span> {findings.length} hole{findings.length === 1 ? "" : "s"} that could let money leak</summary>
    <ul className="security-findings">{findings.map((finding) => <li key={finding.name}>
      <span className={`badge ${finding.severity === "high" ? "failed" : "warning"}`}>{finding.severity} risk</span>
      <div><strong>{finding.title}</strong><small>{finding.detail}</small><small className="fix">Fix: {finding.fix}</small></div>
    </li>)}</ul>
  </details>;
}

// PayPal's side of the scan: payments PayPal took for this store that the store never recorded.
function UnrecordedPayments({ report }) {
  return <div className="triage-plan unrecorded">
    <div className="check-row-head"><strong>Payments the store never recorded</strong><span className="count-chip">PayPal Agent Toolkit · transaction search</span></div>
    {report.payments.length > 0
      ? <>
        <p>PayPal took <strong>{report.money} {report.currency}</strong> for this store that the store has no paid order for. The buyers were charged and will not get their order unless you ship it or refund them.</p>
        <ul className="dispute-facts">{report.payments.map((payment) => <li key={payment.transaction_id}>
          <code>{payment.transaction_id}</code> · {payment.amount} {payment.currency}
          {payment.paid_at && <> · {new Date(payment.paid_at).toLocaleString()}</>}
          {payment.payer_email_masked && <> · {payment.payer_email_masked}</>}
        </li>)}</ul>
      </>
      : <p>{report.note}</p>}
  </div>;
}

const COLUMNS = [
  { field: "rank", headerName: "#", width: 60 },
  { field: "order_id", headerName: "Order", width: 175, cellClass: "grid-mono" },
  { field: "title", headerName: "Leak", flex: 1, minWidth: 220, tooltipField: "title" },
  { field: "amount", headerName: "At risk", width: 110, type: "rightAligned" },
  { field: "urgency", headerName: "Urgency", width: 110, cellRenderer: UrgencyCell },
  { field: "why", headerName: "Why now", flex: 1, minWidth: 220, tooltipField: "why" },
  { field: "waiting_since", headerName: "Waiting", width: 250, cellRenderer: WaitingCell, sortable: false },
  { field: "id", headerName: "Recover", width: 290, cellRenderer: ActionsCell, sortable: false, filter: false },
];

export default function ScanPanel({ onRecovered, onScanned, embedded = false }) {
  const [store, setStore] = useState("main");
  const [limit, setLimit] = useState(25);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [outcomes, setOutcomes] = useState({});

  const rows = useMemo(() => (result ? scanRows(result) : []), [result]);
  // After an approval the numbers drop at once, as they do on the overview, instead of waiting for a rescan.
  const remaining = useMemo(() => (result ? remainingAtRisk(result, outcomes) : null), [result, outcomes]);

  async function scan(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    setOutcomes({});
    try {
      setResult(await apiJson("/api/stores/scan", { method: "POST", body: scanBody(store, limit) }, "The scan could not be completed."));
      onScanned?.();
    } catch (reason) {
      setError(withSampleHint(reason.message));
    } finally { setBusy(false); }
  }

  async function approve(row) {
    if (!window.confirm(approvalQuestion(row.leak, row.order_id))) return;
    setOutcomes((current) => ({ ...current, [row.id]: { busy: true } }));
    try {
      const record = await apiJson("/api/recoveries", { method: "POST", body: recoveryBody(row.order_id, result.store, row.leak.kind) }, "The recovery could not be completed.");
      setOutcomes((current) => ({ ...current, [row.id]: { record } }));
      onRecovered?.(record);
    } catch (reason) {
      setOutcomes((current) => ({ ...current, [row.id]: { error: reason.message } }));
    }
  }

  const context = useMemo(() => ({ outcomes, approve, store: result?.store || store }), [outcomes, result, store]); // eslint-disable-line react-hooks/exhaustive-deps

  // The grid does not watch its context, so redraw the rows (Recover cell and the done shading) on every approval.
  const grid = useRef(null);
  useEffect(() => {
    grid.current?.api?.redrawRows();
  }, [context]);

  const Wrapper = embedded ? "div" : "section";
  return <Wrapper className={embedded ? "embedded-panel" : "panel"} id="scan" aria-labelledby="scan-heading">
    <div className="section-heading">
      <div>{!embedded && <p className="eyebrow">PayRecover · whole store</p>}<h2 id="scan-heading">{embedded ? "Check the whole store" : "Scan my store"}</h2></div>
    </div>
    <p className="hint">Every recent paid order, checked against PayPal. Nothing moves until you approve a row.</p>
    <form className="scan-form" onSubmit={scan}>
      <label className="field-label">Store
        <select className="text-input" value={store} onChange={(event) => setStore(event.target.value)}>
          {STORES.map(([key, text]) => <option key={key} value={key}>{text}</option>)}
        </select>
      </label>
      <label className="field-label">Recent orders
        <select className="text-input" value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
          {[10, 25, 50].map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      <button className="danger-button" type="submit" disabled={busy}>
        {busy ? <><span className="spinner" aria-hidden="true" /> Scanning…</> : "Scan store"}
      </button>
    </form>
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {result && <>
      {result.verdict && <Verdict result={result} plan={result.plan} />}
      <div className="scan-stats">
        <article><span>Paid orders checked</span><strong>{result.orders_checked}</strong></article>
        <article><span>Orders still leaking</span><strong>{remaining.orders}</strong></article>
        <article className={Number(remaining.money) > 0 ? "risk" : "good"}><span>Money still at risk</span><strong>{remaining.money} {result.currency}</strong></article>
      </div>
      {rows.length > 0
        ? <div className="recovery-grid"><AgGridReact ref={grid} theme={gridTheme} rowData={rows} columnDefs={COLUMNS} context={context}
            getRowId={({ data }) => data.id} domLayout="autoHeight" rowHeight={44} suppressColumnVirtualisation
            rowClassRules={{ "row-done": ({ data, context: current }) => Boolean(current.outcomes[data.id]?.record) }}
            defaultColDef={{ sortable: true, filter: true, resizable: true }} /></div>
        : <p className="empty-state">No leaks: every paid order in this store matches PayPal&apos;s record.</p>}
      {result.unrecorded && <UnrecordedPayments report={result.unrecorded} />}
      <StoreSecurity security={result.security} open={result.verdict === "vulnerable"} />
    </>}
    {/* A separate check, so it comes after the scan's own results. */}
    <MarketPrices store={store} />
  </Wrapper>;
}
