import { useEffect, useState } from "react";
import { apiJson } from "../api/client.js";
import { alertLabel, alertSettingsBody, timeAgo } from "../lib/assist.js";

const REFRESH_MS = 30000;
const LINKS = { leak_found: "#scan", scan_done: "#scan", recovery_done: "#recoveries", dispute_opened: "#disputes", unrecorded_payment: "#scan" };

function Channels({ channels }) {
  const entries = Object.entries(channels || {});
  if (!entries.length) return <small className="hint">Dashboard only</small>;
  return <small className="hint">{entries.map(([name, state]) => `${name === "email" ? "Email" : "Zapier"}: ${state}`).join(" · ")}</small>;
}

export default function AlertsPanel({ refreshKey }) {
  const [view, setView] = useState(null);
  const [email, setEmail] = useState("");
  const [zapier, setZapier] = useState("");
  const [busy, setBusy] = useState(null);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState(null);

  const fetchAlerts = () => apiJson("/api/alerts", {}, "Alerts could not be loaded.");
  const load = () => fetchAlerts().then(setView).catch((reason) => setError(reason.message));

  useEffect(() => {
    fetchAlerts().then((data) => {
      setView(data);
      setEmail(data.settings.email || "");
      setZapier(data.settings.zapier_url || "");
    }).catch((reason) => setError(reason.message));
  }, [refreshKey]);
  useEffect(() => {
    const timer = setInterval(() => { if (!document.hidden) fetchAlerts().then(setView).catch(() => {}); }, REFRESH_MS);
    return () => clearInterval(timer);
  }, []);

  async function run(kind, action, success) {
    setBusy(kind);
    setError(null);
    setNotice(null);
    try {
      await action();
      setNotice(success);
      await load();
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(null); }
  }

  const save = (event) => {
    event.preventDefault();
    run("save", () => apiJson("/api/alerts/settings", { method: "PUT", body: alertSettingsBody(email, zapier) }, "The alert settings could not be saved."),
      "Saved. New alerts go to the dashboard" + (email.trim() && view?.email_available ? ", your email" : "")
        + (zapier.trim() ? " and your Zap" : "") + ".");
  };
  const test = () => run("test", () => apiJson("/api/alerts/test", { method: "POST" }, "The test alert could not be sent."), "Test alert sent. Check the list below and your inbox.");
  const clear = () => run("clear", () => apiJson("/api/alerts", { method: "DELETE" }, "The alerts could not be cleared."), "Alert list cleared.");

  return <section className="panel" id="alerts" aria-labelledby="alerts-heading">
    <div className="section-heading">
      <div><p className="eyebrow">PayRecover · email + Zapier</p><h2 id="alerts-heading">Alerts</h2></div>
      {view?.alerts.length > 0 && <button className="secondary-button" type="button" onClick={clear} disabled={Boolean(busy)}>Clear list</button>}
    </div>
    <p className="hint">PayRecover tells you when money leaks, a payment goes unrecorded, a dispute opens or a recovery finishes: always here, by email if you add one, and anywhere else through your own Zap.</p>
    <form className="alert-settings" onSubmit={save}>
      <label className="field-label">Email me at
        <input className="text-input" type="email" value={email} maxLength={254} placeholder="you@example.com"
          onChange={(event) => setEmail(event.target.value)} />
      </label>
      <label className="field-label">Also send to Zapier <span className="hint">(optional: Slack, SMS, 9,000+ apps)</span>
        <input className="text-input" type="url" value={zapier} maxLength={300} placeholder="https://hooks.zapier.com/hooks/catch/…"
          onChange={(event) => setZapier(event.target.value)} />
      </label>
      <div className="alert-buttons">
        <button className="danger-button" type="submit" disabled={Boolean(busy)}>{busy === "save" ? "Saving…" : "Save"}</button>
        <button className="secondary-button" type="button" onClick={test} disabled={Boolean(busy)}>{busy === "test" ? "Sending…" : "Send test alert"}</button>
      </div>
    </form>
    {view && !view.email_available && email.trim() && <p className="notice" role="status">Email sending is not set up on this server yet, so alerts stay on the dashboard{view.zapier_default || zapier.trim() ? " and Zapier" : ""}.</p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {view && (view.alerts.length
      ? <ul className="alert-feed">{view.alerts.map((alert) => {
        const [label, tone] = alertLabel(alert.event);
        return <li key={alert.alert_id}>
          <span className={`badge ${tone}`}>{label}</span>
          <div>
            <a href={LINKS[alert.event] || "#dashboard"}>{alert.summary}</a>
            <Channels channels={alert.channels} />
          </div>
          <small className="hint" title={new Date(alert.created_at).toLocaleString()}>{timeAgo(alert.created_at)}</small>
        </li>;
      })}</ul>
      : <p className="empty-state">No alerts yet. Scan a store, or press Send test alert.</p>)}
  </section>;
}
