import { useEffect, useState } from "react";
import { apiJson } from "../api/client.js";
import { AUTOSCAN_INTERVALS, autoscanText, storeStatusText, storeVerdict, timeAgo, withSampleHint } from "../lib/assist.js";
import { STORES } from "../lib/payment.js";
import PayPalSetup from "./PayPalSetup.jsx";

const REFRESH_MS = 30000;
const KEEP_AWAKE_MS = 10 * 60 * 1000;
const STORE_NAMES = Object.fromEntries(STORES.map(([key, label]) => [key, label.replace(/ \(.*\)$/, "")]));

// Headline numbers, each store's latest result, and a live indicator, refreshed every 30 seconds.
export default function OverviewPanel({ refreshKey }) {
  const [data, setData] = useState(null);
  const [scanning, setScanning] = useState({});
  const [error, setError] = useState(null);
  const [now, setNow] = useState(() => Date.now());
  // Each demo store's start-up state: "starting" until it answers, then "up" or "down".
  const [startup, setStartup] = useState(() => Object.fromEntries(STORES.map(([store]) => [store, "starting"])));

  // Opening the dashboard starts every demo store (a free host puts idle ones to sleep), and keeps them
  // awake every few minutes while the dashboard is open, so a scan never meets a sleeping store.
  useEffect(() => {
    const start = () => apiJson("/api/stores/start", { method: "POST" })
      .then((pings) => setStartup(Object.fromEntries(pings.map((ping) => [ping.store, ping.up ? "up" : "down"]))))
      .catch(() => setStartup(Object.fromEntries(STORES.map(([store]) => [store, "up"]))));
    start();
    const timer = setInterval(() => { if (!document.hidden) start(); }, KEEP_AWAKE_MS);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    apiJson("/api/overview").then(setData).catch(() => setData(null));
    // A recovery re-checks its store in the background; read the overview again once that is done.
    const later = setTimeout(() => apiJson("/api/overview").then(setData).catch(() => {}), 6000);
    return () => clearTimeout(later);
  }, [refreshKey]);
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
      if (!document.hidden) apiJson("/api/overview").then(setData).catch(() => {});
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, []);

  // Clicking a store scans it right away (payments and the security check, without the AI plan, so it is quick).
  async function scanStore(store) {
    setScanning((current) => ({ ...current, [store]: true }));
    setError(null);
    try {
      await apiJson("/api/stores/scan", { method: "POST", body: { store, explain: false } }, "The scan could not be completed.");
      setData(await apiJson("/api/overview"));
    } catch (reason) {
      setError(withSampleHint(reason.message));
    } finally {
      setScanning((current) => ({ ...current, [store]: false }));
    }
  }

  async function setAutoscan(enabled, interval) {
    setError(null);
    try {
      await apiJson("/api/autoscan", { method: "PUT", body: { enabled, interval_minutes: interval } }, "Auto-scan could not be changed.");
      setData(await apiJson("/api/overview"));
    } catch (reason) {
      setError(reason.message);
    }
  }

  const auto = data?.autoscan;
  const money = (value) => (data ? `${value} ${data.currency}` : "–");
  const cards = [
    ["Orders scanned", data ? data.orders_scanned : "–", "⌕", "Latest scan of each store", ""],
    ["Money at risk", money(data?.money_at_risk), "!", "Waiting for your approval", data && Number(data.money_at_risk) > 0 ? "risk" : ""],
    ["Open disputes", data ? (data.open_disputes ?? "?") : "–", "⚖", data && data.open_disputes === null ? "PayPal not reachable" : "From PayPal", ""],
    ["Money recovered", money(data?.money_recovered), "↺", "Captured, invoiced, refunded", "good"],
  ];

  return <section className="overview" aria-label="Overview">
    <div className="overview-bar">
      <div className="live-indicator" role="status">
        <i aria-hidden="true" />
        <span>Live{data?.last_activity ? ` · last check ${timeAgo(data.last_activity, now)}` : " · no checks yet"}</span>
      </div>
      {auto && <div className={`autoscan ${auto.enabled ? "on" : ""}`}>
        <label className="switch">
          <input type="checkbox" checked={auto.enabled} onChange={(event) => setAutoscan(event.target.checked, auto.interval_minutes)} />
          <span aria-hidden="true" />
          <span className="sr-only">Auto-scan every store</span>
        </label>
        <span role="status">{autoscanText(auto, now)}</span>
        <select className="text-input" aria-label="How often to auto-scan" value={auto.interval_minutes}
          onChange={(event) => setAutoscan(auto.enabled, Number(event.target.value))}>
          {AUTOSCAN_INTERVALS.map((minutes) => <option key={minutes} value={minutes}>Every {minutes} min</option>)}
        </select>
      </div>}
    </div>
    <PayPalSetup />
    <div className="metrics overview-cards">
      {cards.map(([title, value, icon, note, tone]) => <article className={`metric-card ${tone}`} key={title}>
        <span className="metric-icon" aria-hidden="true">{icon}</span>
        <div><p>{title}</p><strong>{value}</strong><small>{note}</small></div>
      </article>)}
    </div>
    <div className="demo-stores-heading">
      <strong>Demo stores · PayPal Sandbox</strong>
      <span>Click a store to scan it now.</span>
    </div>
    <div className="store-statuses" aria-label="Demo store results">
      {(data?.stores || STORES.map(([store]) => ({ store }))).map((status) => {
        const [label, tone] = storeVerdict(status);
        const busy = scanning[status.store];
        const starting = startup[status.store] === "starting";
        const down = startup[status.store] === "down";
        const [title, detail] = busy ? ["Scanning…", "Checking payments and security"]
          : starting ? ["The store is starting", "Free hosting can take up to a minute"]
            : down ? ["Not responding", "Click to try again"]
              : data ? [label, storeStatusText(status, now)] : ["Loading…", "Reading the latest result"];
        return <button type="button" className={`store-status ${busy || starting ? "status-scanning" : down ? "status-none" : tone}`}
          key={status.store} disabled={busy || starting} onClick={() => scanStore(status.store)} title="Scan this store now">
          <i aria-hidden="true" />
          <span><strong>{STORE_NAMES[status.store] || status.store}</strong> · {title}</span>
          <small>{detail}</small>
        </button>;
      })}
    </div>
    {error && <p className="notice notice-error" role="alert">{error}</p>}
  </section>;
}
