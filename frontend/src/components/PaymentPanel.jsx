import { useEffect, useState } from "react";
import { apiJson } from "../api/client.js";
import { OUTCOMES, PAYMENT_CHECK_LABELS, emptyPaymentForm, paymentBody, paymentProblems } from "../lib/payment.js";

const ORDER_FIELDS = [
  ["status", "PayPal status"],
  ["amount", "Order amount"],
  ["currency", "Currency"],
  ["capture_id", "Capture id"],
  ["capture_status", "Capture status"],
  ["capture_amount", "Captured amount"],
  ["update_time", "Last updated at PayPal"],
];

function Verification({ result }) {
  const outcome = result.verified ? "passed" : "failed";
  return <div className="payment-result">
    <div className="check-row-head">
      <div>
        <strong>Order <code>{result.order_id}</code></strong>
        <small>Source: PayPal Sandbox Orders API · {new Date(result.checked_at).toLocaleString()}</small>
      </div>
      <span className={`badge ${outcome}`}>{result.verified ? "Verified" : "Not verified"}</span>
    </div>
    {result.checks.map((check) => <div className="check-row" key={check.name}>
      <div className="check-row-head">
        <strong>{PAYMENT_CHECK_LABELS[check.name] || check.name}</strong>
        <span className={`badge ${check.status}`}>{check.status}</span>
      </div>
      <small>{check.message}</small>
    </div>)}
    {result.order && <dl className="payment-order">
      {ORDER_FIELDS.filter(([key]) => result.order[key] !== null && result.order[key] !== undefined).map(([key, text]) => <div key={key}>
        <dt>{text}</dt><dd>{String(result.order[key])}</dd>
      </div>)}
    </dl>}
    <details className="raw-evidence"><summary>Raw evidence</summary><pre>{JSON.stringify(result.checks, null, 2)}</pre></details>
  </div>;
}

export default function PaymentPanel() {
  const [config, setConfig] = useState(null);
  const [form, setForm] = useState(emptyPaymentForm());
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    apiJson("/api/paypal/status").then(setConfig).catch(() => setConfig(null));
  }, []);

  const set = (change) => setForm((current) => ({ ...current, ...change }));
  const problems = paymentProblems(form);

  async function verify(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await apiJson("/api/paypal/verify", { method: "POST", body: paymentBody(form) }, "PayPal verification failed."));
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(false); }
  }

  return <section className="panel" id="payments" aria-labelledby="payments-heading">
    <div className="section-heading">
      <div><p className="eyebrow">PayPal Sandbox</p><h2 id="payments-heading">Verify a payment</h2></div>
      <span className={`status-pill ${config?.configured ? "online" : ""}`}><i />{config ? (config.configured ? "Sandbox connected" : "Sandbox keys missing") : "Sandbox status unknown"}</span>
    </div>
    <p className="hint">Paste the order id the store showed. PayTest asks PayPal itself, not the store and not Gemma, whether the money was really captured.</p>
    <form className="payment-form" onSubmit={verify}>
      <label className="field-label">Order id
        <input className="text-input" type="text" required placeholder="e.g. 5O190127TN364715T" value={form.orderId} onChange={(event) => set({ orderId: event.target.value })} />
      </label>
      <label className="field-label">Expected outcome
        <select className="text-input" value={form.outcome} onChange={(event) => set({ outcome: event.target.value })}>
          {OUTCOMES.map(([key, text]) => <option key={key} value={key}>{text}</option>)}
        </select>
      </label>
      <label className="field-label">Expected amount (optional)
        <input className="text-input" type="text" inputMode="decimal" placeholder="e.g. 18.00" value={form.expectedAmount} onChange={(event) => set({ expectedAmount: event.target.value })} />
      </label>
      <label className="field-label">Currency
        <input className="text-input" type="text" maxLength={3} value={form.currency} onChange={(event) => set({ currency: event.target.value })} />
      </label>
      <button className="danger-button" type="submit" disabled={busy || !form.orderId.trim() || problems.length > 0 || config?.configured === false}>
        {busy ? <><span className="spinner" aria-hidden="true" /> Asking PayPal…</> : "Verify with PayPal"}
      </button>
    </form>
    {problems.length > 0 && <ul className="problem-list">{problems.map((problem) => <li key={problem}>{problem}</li>)}</ul>}
    {config?.configured === false && <p className="notice notice-error" role="status">Set TESTPILOT_PAYPAL_CLIENT_ID and TESTPILOT_PAYPAL_CLIENT_SECRET in backend/.env, then restart the backend.</p>}
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {result && <Verification result={result} />}
    <p className="hint">One read-only request to the PayPal Sandbox Orders API. Payer details are never shown or stored.</p>
  </section>;
}
