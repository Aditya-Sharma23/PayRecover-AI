import { useMemo, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import { apiJson } from "../api/client.js";
import { gridTheme } from "../lib/grid.js";
import {
  RECOVERY_LABELS, RECOVERY_STATUS, STORES, URGENCY, approvalQuestion, recoveryBody, scanBody, scanRows,
} from "../lib/payment.js";
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

const COLUMNS = [
  { field: "rank", headerName: "#", width: 60 },
  { field: "order_id", headerName: "Order", width: 175, cellClass: "grid-mono" },
  { field: "title", headerName: "Leak", flex: 1, minWidth: 220, tooltipField: "title" },
  { field: "amount", headerName: "At risk", width: 110, type: "rightAligned" },
  { field: "urgency", headerName: "Urgency", width: 110, cellRenderer: UrgencyCell },
  { field: "waiting_since", headerName: "Waiting", width: 270, cellRenderer: WaitingCell, sortable: false },
  { field: "id", headerName: "Recover", width: 290, cellRenderer: ActionsCell, sortable: false, filter: false },
];

export default function ScanPanel({ onRecovered }) {
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

  return <section className="panel" id="scan" aria-labelledby="scan-heading">
    <div className="section-heading">
      <div><p className="eyebrow">PayRecover · whole store</p><h2 id="scan-heading">Scan my store</h2></div>
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
        ? <div className="recovery-grid"><AgGridReact theme={gridTheme} rowData={rows} columnDefs={COLUMNS} context={context}
            getRowId={({ data }) => data.id} domLayout="autoHeight" rowHeight={44}
            defaultColDef={{ sortable: true, filter: true, resizable: true }} /></div>
        : <p className="empty-state">No leaks: every paid order in this store matches PayPal&apos;s record.</p>}
    </>}
  </section>;
}
