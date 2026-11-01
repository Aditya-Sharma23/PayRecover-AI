import { useEffect, useState } from "react";
import { apiJson } from "../api/client.js";
import { STORE_VERDICTS, storeStatusText, timeAgo } from "../lib/assist.js";
import { STORES } from "../lib/payment.js";

const REFRESH_MS = 30000;
const STORE_NAMES = Object.fromEntries(STORES.map(([key, label]) => [key, label.replace(/ \(.*\)$/, "")]));

// Headline numbers, each store's latest result, and a live indicator, refreshed every 30 seconds.
export default function OverviewPanel({ refreshKey }) {
  const [data, setData] = useState(null);
  const [scanning, setScanning] = useState({});
  const [error, setError] = useState(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    apiJson("/api/overview").then(setData).catch(() => setData(null));
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
      setError(reason.message);
    } finally {
      setScanning((current) => ({ ...current, [store]: false }));
    }
  }

  const money = (value) => (data ? `${value} ${data.currency}` : "–");
  const cards = [
    ["Orders scanned", data ? data.orders_scanned : "–", "⌕", "Latest scan of each store", ""],
    ["Money at risk", money(data?.money_at_risk), "!", "Waiting for your approval", data && Number(data.money_at_risk) > 0 ? "risk" : ""],
    ["Open disputes", data ? (data.open_disputes ?? "?") : "–", "⚖", data && data.open_disputes === null ? "PayPal not reachable" : "From PayPal", ""],
    ["Money recovered", money(data?.money_recovered), "↺", "Captured, invoiced, refunded", "good"],
  ];

  return <section className="overview" aria-label="Overview">
    <div className="live-indicator" role="status">
      <i aria-hidden="true" />
      <span>Live{data?.last_activity ? ` · last check ${timeAgo(data.last_activity, now)}` : " · no checks yet"}</span>
    </div>
    <div className="metrics overview-cards">
      {cards.map(([title, value, icon, note, tone]) => <article className={`metric-card ${tone}`} key={title}>
        <span className="metric-icon" aria-hidden="true">{icon}</span>
        <div><p>{title}</p><strong>{value}</strong><small>{note}</small></div>
      </article>)}
    </div>
    <div className="store-statuses" aria-label="Store results">
      {(data?.stores || STORES.map(([store]) => ({ store }))).map((status) => {
        const [label, tone] = STORE_VERDICTS[status.verdict] || STORE_VERDICTS.none;
        const busy = scanning[status.store];
        return <button type="button" className={`store-status ${busy ? "status-scanning" : tone}`} key={status.store}
          disabled={busy} onClick={() => scanStore(status.store)} title="Scan this store now">
          <i aria-hidden="true" />
          <span><strong>{STORE_NAMES[status.store] || status.store}</strong> · {busy ? "Scanning…" : label}</span>
          <small>{busy ? "Checking payments and security" : storeStatusText(status, now)}</small>
        </button>;
      })}
    </div>
    {error && <p className="notice notice-error" role="alert">{error}</p>}
  </section>;
}
