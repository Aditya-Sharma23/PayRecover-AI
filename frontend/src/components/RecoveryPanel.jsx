import { useEffect, useMemo, useState } from "react";
import { AllCommunityModule, ModuleRegistry, colorSchemeDark, themeQuartz } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { apiJson } from "../api/client.js";
import { RECOVERY_LABELS, RECOVERY_STATUS, recoveryRows } from "../lib/payment.js";

ModuleRegistry.registerModules([AllCommunityModule]);

// The dashboard's dark palette, so the grid reads as part of the page.
const theme = themeQuartz.withPart(colorSchemeDark).withParams({
  backgroundColor: "#11161d",
  foregroundColor: "#e9edf5",
  headerBackgroundColor: "#0c1117",
  borderColor: "#252c35",
  accentColor: "#ed2938",
  fontFamily: "inherit",
  fontSize: 12,
  headerFontWeight: 700,
});

function StatusCell({ value }) {
  const [label, tone] = RECOVERY_STATUS[value] || RECOVERY_STATUS.failed;
  return <span className={`badge ${tone}`}>{label}</span>;
}

const COLUMNS = [
  { field: "when", headerName: "When", sort: "desc", width: 150 },
  { field: "order_id", headerName: "Order", width: 175, cellClass: "grid-mono" },
  { field: "store", headerName: "Store", width: 105 },
  { field: "leak", headerName: "Leak", width: 150 },
  { field: "action", headerName: "Action", width: 150, valueFormatter: ({ value }) => RECOVERY_LABELS[value] || value },
  { field: "amount", headerName: "Amount", width: 115, type: "rightAligned", comparator: (a, b) => parseFloat(a) - parseFloat(b) },
  { field: "status", headerName: "Status", width: 135, cellRenderer: StatusCell },
  { field: "message", headerName: "Result", flex: 1, minWidth: 240, tooltipField: "message" },
];

export default function RecoveryPanel({ refreshKey }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    apiJson("/api/recoveries").then((value) => { setData(value); setError(null); }).catch((reason) => setError(reason.message));
  }, [refreshKey]);

  const rows = useMemo(() => recoveryRows(data?.records || []), [data]);
  const totals = data?.totals || { collected: "0.00", invoiced: "0.00", refunded: "0.00" };

  return <section className="panel" id="recoveries" aria-labelledby="recoveries-heading">
    <div className="section-heading">
      <div><p className="eyebrow">Approved and verified at PayPal</p><h2 id="recoveries-heading">Money recovered</h2></div>
      <span className="count-chip">{rows.length} recover{rows.length === 1 ? "y" : "ies"}</span>
    </div>
    <div className="recovery-totals">
      <article><span>Collected</span><strong>{totals.collected} USD</strong><small>Captured payments the store had missed</small></article>
      <article><span>Invoiced</span><strong>{totals.invoiced} USD</strong><small>Sent to buyers who paid too little</small></article>
      <article><span>Refunded</span><strong>{totals.refunded} USD</strong><small>Duplicate charges returned before a chargeback</small></article>
    </div>
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {rows.length > 0
      ? <div className="recovery-grid"><AgGridReact theme={theme} rowData={rows} columnDefs={COLUMNS} domLayout="autoHeight"
          defaultColDef={{ sortable: true, filter: true, resizable: true }} pagination paginationPageSize={10}
          paginationPageSizeSelector={false} /></div>
      : <p className="empty-state">No recoveries yet. When an investigation finds a leak, approve its recovery and it appears here.</p>}
  </section>;
}
