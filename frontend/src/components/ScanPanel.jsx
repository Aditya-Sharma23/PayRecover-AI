import { useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import { apiJson } from "../api/client.js";
import { gridTheme } from "../lib/grid.js";
import {
  RECOVERY_LABELS, RECOVERY_STATUS, STORES, URGENCY, approvalQuestion, recoveryBody, scanBody, scanRows,
} from "../lib/payment.js";
import { SCAN_VERDICTS } from "../lib/assist.js";
import { EvidenceButton, WaitingClock } from "./PaymentPanel.jsx";

function UrgencyCell({ value }) {
  const [label, tone] = URGENCY[value] || URGENCY.normal;
  return <span className={`badge ${tone}`}>{label}</span>;
}

function WaitingCell({ value }) {
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

function Verdict({ result }) {
  const [title, tone, text] = SCAN_VERDICTS[result.verdict] || SCAN_VERDICTS.clear;
  const findings = result.security?.findings || [];
  return <div className={`scan-verdict ${tone}`} role="status">
    <strong>{title}</strong>
    <p>{text}</p>
    {findings.length > 0 && <ul className="security-findings">{findings.map((finding) => <li key={finding.name}>
      <span className={`badge ${finding.severity === "high" ? "failed" : "warning"}`}>{finding.severity} risk</span>
      <div><strong>{finding.title}</strong><small>{finding.detail}</small><small className="fix">Fix: {finding.fix}</small></div>
    </li>)}</ul>}
    {result.security && !result.security.checked && <small className="hint">Security check incomplete: {result.security.note}</small>}
  </div>;
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
  { field: "waiting_since", headerName: "Waiting", width: 270, cellRenderer: WaitingCell, sortable: false },
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
      setError(reason.message);
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

  // The grid does not watch its context, so redraw the Recover cells whenever an approval changes state.
  const grid = useRef(null);
  useEffect(() => {
    grid.current?.api?.refreshCells({ columns: ["id"], force: true });
  }, [context]);

  const Wrapper = embedded ? "div" : "section";
  return <Wrapper className={embedded ? "embedded-panel" : "panel"} id="scan" aria-labelledby="scan-heading">
    <div className="section-heading">
      <div>{!embedded && <p className="eyebrow">PayRecover · whole store</p>}<h2 id="scan-heading">{embedded ? "Check the whole store" : "Scan my store"}</h2></div>
    </div>
    <p className="hint">Checks every recent order the store calls paid against PayPal, ranks the leaks by urgency and amount, and has the AI write the recovery plan. Nothing moves until you approve a row.</p>
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
      {result.verdict && <Verdict result={result} />}
      <div className="scan-stats">
        <article><span>Paid orders checked</span><strong>{result.orders_checked}</strong></article>
        <article><span>Orders leaking</span><strong>{result.leaking_orders}</strong></article>
        <article className="risk"><span>Money at risk</span><strong>{result.money_at_risk} {result.currency}</strong></article>
      </div>
      <div className="triage-plan">
        <div className="check-row-head"><strong>Recovery plan</strong><span className="count-chip">{result.plan.source}</span></div>
        <p>{result.plan.summary}</p>
        {result.plan.steps.length > 0 && <ol>{result.plan.steps.map((step) => <li key={`${step.order_id}-${step.kind}`}><code>{step.order_id}</code> · {step.amount} {result.currency} · {step.why}</li>)}</ol>}
        {result.plan.notes.length > 0 && <small className="hint">{result.plan.notes.join(" ")}</small>}
      </div>
      {rows.length > 0
        ? <div className="recovery-grid"><AgGridReact ref={grid} theme={gridTheme} rowData={rows} columnDefs={COLUMNS} context={context}
            getRowId={({ data }) => data.id} domLayout="autoHeight" rowHeight={44} suppressColumnVirtualisation
            defaultColDef={{ sortable: true, filter: true, resizable: true }} /></div>
        : <p className="empty-state">No leaks: every paid order in this store matches PayPal&apos;s record.</p>}
      {result.unrecorded && <UnrecordedPayments report={result.unrecorded} />}
    </>}
  </Wrapper>;
}
