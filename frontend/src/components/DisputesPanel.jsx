import { useState } from "react";
import { apiJson } from "../api/client.js";
import { DISPUTE_ACTIONS, disputeBody, disputeQuestion, disputeReason } from "../lib/assist.js";
import { EvidenceButton } from "./PaymentPanel.jsx";

const ACTION_BUTTONS = { provide_evidence: "Approve: send evidence", accept_claim: "Approve: accept claim" };

function DisputeCard({ dispute, onAnswered }) {
  const advice = dispute.advice;
  const [note, setNote] = useState(advice?.note || "");
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [label, tone] = DISPUTE_ACTIONS[advice?.recommendation] || DISPUTE_ACTIONS.review;

  async function answer(action) {
    if (!window.confirm(disputeQuestion(dispute, action))) return;
    setBusy(action);
    setError(null);
    try {
      const sent = await apiJson(`/api/disputes/${encodeURIComponent(dispute.dispute_id)}/respond`,
        { method: "POST", body: disputeBody(action, note) }, "PayPal did not accept the answer.");
      setResult(sent);
      onAnswered?.(sent);
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(null); }
  }

  return <article className="dispute-card">
    <div className="check-row-head">
      <strong>{disputeReason(dispute.reason)} · {dispute.amount} {dispute.currency}</strong>
      <span className="badge skipped">{(dispute.status || "").replaceAll("_", " ").toLowerCase()}</span>
    </div>
    <p className="hint">
      Dispute <code>{dispute.dispute_id}</code>
      {dispute.order_id && <> · order <code>{dispute.order_id}</code> ({dispute.store} store)</>}
      {dispute.respond_by && <> · respond by {new Date(dispute.respond_by).toLocaleDateString()}</>}
    </p>
    <ul className="dispute-facts">{dispute.facts.map((fact) => <li key={fact}>{fact}</li>)}</ul>
    {advice && <div className="triage-plan">
      <div className="check-row-head"><strong>Recommended: <span className={`badge ${tone}`}>{label}</span></strong><span className="count-chip">{advice.source}</span></div>
      <ul className="dispute-facts">{advice.reasoning.map((reason) => <li key={reason}>{reason}</li>)}</ul>
      <label className="field-label">Message to PayPal (edit before approving)
        <textarea rows={4} value={note} maxLength={2000} onChange={(event) => setNote(event.target.value)} />
      </label>
    </div>}
    {result
      ? <p className={`notice ${result.status === "failed" ? "notice-error" : ""}`} role="status">{result.message} {result.dispute_status_after && <>PayPal now shows: <strong>{result.dispute_status_after}</strong>.</>}</p>
      : <div className="grid-actions dispute-actions">
        {dispute.allowed_actions.map((action) => <button key={action} type="button" disabled={Boolean(busy) || note.trim().length < 10}
          className={action === advice?.recommendation ? "danger-button" : "secondary-button"} onClick={() => answer(action)}>
          {busy === action ? "Asking PayPal…" : ACTION_BUTTONS[action]}
        </button>)}
        {dispute.order_id && <EvidenceButton orderId={dispute.order_id} store={dispute.store || "main"} />}
        {dispute.allowed_actions.length === 0 && <span className="hint">PayPal offers no answer for this dispute now.</span>}
      </div>}
    {error && <p className="notice notice-error" role="alert">{error}</p>}
  </article>;
}

export default function DisputesPanel({ onAnswered }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function load() {
    setBusy(true);
    setError(null);
    try {
      setData(await apiJson("/api/disputes", {}, "Disputes could not be loaded."));
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(false); }
  }

  return <section className="panel" id="disputes" aria-labelledby="disputes-heading">
    <div className="section-heading">
      <div><p className="eyebrow">PayRecover · PayPal Agent Toolkit</p><h2 id="disputes-heading">Dispute defender</h2></div>
      <button className="secondary-button" type="button" onClick={load} disabled={busy}>{busy ? "Reading PayPal…" : data ? "Refresh" : "Load disputes"}</button>
    </div>
    <p className="hint">Reads your PayPal disputes with PayPal&apos;s Agent Toolkit, links each one to its store order, and has the AI recommend an answer from the evidence. You edit the message and approve; PayRecover then confirms the result with PayPal.</p>
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {data && (data.disputes.length
      ? <div className="dispute-list">{data.disputes.map((dispute) => <DisputeCard key={dispute.dispute_id} dispute={dispute} onAnswered={onAnswered} />)}</div>
      : <p className="empty-state">{data.note || "No disputes."}</p>)}
  </section>;
}
