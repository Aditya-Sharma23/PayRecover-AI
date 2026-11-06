import { useEffect, useState } from "react";
import { apiJson } from "../api/client.js";
import { connectorBody, mappingRows } from "../lib/fixes.js";

const EMPTY = { name: "", base_url: "", orders_path: "", products_path: "" };

// Connect your own store: the AI works out how to read its orders; PayRecover checks that against every order
// and against PayPal, and only a mapping that passes is saved.
export default function ConnectorPanel({ onConnected }) {
  const [status, setStatus] = useState(null);
  const [form, setForm] = useState(EMPTY);
  const [proposal, setProposal] = useState(null);
  const [edited, setEdited] = useState("");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  const load = () => apiJson("/api/connector").then(setStatus).catch(() => setStatus(null));
  useEffect(() => { apiJson("/api/connector").then(setStatus).catch(() => setStatus(null)); }, []);

  const field = (key) => ({ value: form[key], onChange: (event) => setForm({ ...form, [key]: event.target.value }) });

  async function run(kind, action) {
    setBusy(kind);
    setError(null);
    try { await action(); } catch (reason) { setError(reason.message); } finally { setBusy(null); }
  }

  const propose = (mapping = null) => run(mapping ? "check" : "propose", async () => {
    const result = await apiJson("/api/connector/propose", { method: "POST", body: connectorBody(form, mapping) },
      "The store could not be read.");
    setProposal(result);
    setEdited(JSON.stringify(result.mapping, null, 2));
  });

  const checkEdit = () => {
    let mapping;
    try { mapping = JSON.parse(edited); } catch { setError("The mapping is not valid JSON."); return; }
    propose(mapping);
  };

  const save = () => run("save", async () => {
    await apiJson("/api/connector", { method: "PUT", body: proposal.mapping }, "The store could not be connected.");
    setProposal(null);
    await load();
    onConnected?.();
  });

  const disconnect = () => run("forget", async () => {
    await apiJson("/api/connector", { method: "DELETE" }, "The store could not be disconnected.");
    await load();
    onConnected?.();
  });

  if (status?.connected) {
    const mapping = status.mapping;
    return <div className="connector">
      <p className="notice fix-verified" role="status"><span className="badge passed">Connected</span> <b>{mapping.name}</b> at <code>{mapping.base_url}</code>. Pick <b>Your connected store</b> in Whole store or One order to check it; PayRecover only reads it.</p>
      <table className="mapping-table"><tbody>{mappingRows(mapping).map(([label, value]) => <tr key={label}><th>{label}</th><td><code>{value}</code></td></tr>)}</tbody></table>
      <button className="secondary-button danger" type="button" disabled={Boolean(busy)} onClick={disconnect}>Disconnect</button>
      {error && <p className="notice notice-error" role="alert">{error}</p>}
    </div>;
  }

  return <div className="connector">
    <p className="hint">Give PayRecover your store&apos;s orders address. The AI works out how to read it; PayRecover then checks that on every order and asks PayPal about the paid ones. It only reads your store.</p>
    {status?.example && <button className="secondary-button" type="button" onClick={() => setForm({ ...EMPTY, ...status.example, products_path: status.example.products_path || "" })}>
      Try the demo: a shop in another platform&apos;s format
    </button>}
    <div className="connector-form">
      <label className="field-label">Store name<input className="text-input" placeholder="My shop" {...field("name")} /></label>
      <label className="field-label">Store address<input className="text-input" placeholder="https://shop.example.com" {...field("base_url")} /></label>
      <label className="field-label">Orders path<input className="text-input" placeholder="/api/orders" {...field("orders_path")} /></label>
      <label className="field-label">Products path <span className="hint">(optional)</span><input className="text-input" placeholder="/api/products" {...field("products_path")} /></label>
    </div>
    <button className="danger-button" type="button" disabled={Boolean(busy) || !form.base_url.trim() || !form.orders_path.trim()} onClick={() => propose()}>
      {busy === "propose" ? <><span className="spinner" aria-hidden="true" /> The AI is reading your orders…</> : "Let the AI read my store"}
    </button>
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {proposal && <div className="connector-proposal">
      <div className="check-row-head"><strong>How PayRecover will read this store</strong><span className="count-chip">Proposed by {proposal.source}</span></div>
      <table className="mapping-table"><tbody>{mappingRows(proposal.mapping).map(([label, value]) => <tr key={label}><th>{label}</th><td><code>{value}</code></td></tr>)}</tbody></table>
      <strong className="connector-subhead">PayRecover&apos;s checks</strong>
      <ul className="fix-checks">{proposal.checks.map((check, index) => <li key={check.name + index}>
        <span className={`badge ${check.status}`}>{check.status === "passed" ? "✓" : check.status === "failed" ? "✗" : "–"}</span> {check.message}
      </li>)}</ul>
      {proposal.preview.length > 0 && <>
        <strong className="connector-subhead">Your first orders, read this way</strong>
        <table className="mapping-table preview"><thead><tr><th>PayPal order</th><th>Status</th><th>Total</th></tr></thead>
          <tbody>{proposal.preview.map((order) => <tr key={order.order_id}><td><code>{order.order_id}</code></td><td>{order.status}</td><td>{order.total} {order.currency}</td></tr>)}</tbody></table>
      </>}
      <details className="connector-edit"><summary>Edit the mapping yourself</summary>
        <textarea rows={14} value={edited} onChange={(event) => setEdited(event.target.value)} spellCheck={false} />
        <button className="secondary-button" type="button" disabled={Boolean(busy)} onClick={checkEdit}>{busy === "check" ? "Checking…" : "Check my edit"}</button>
      </details>
      <button className="danger-button" type="button" disabled={Boolean(busy) || !proposal.ready} onClick={save}>
        {busy === "save" ? "Checking again…" : proposal.ready ? "Connect this store" : "Fix the failed checks to connect"}
      </button>
    </div>}
  </div>;
}
