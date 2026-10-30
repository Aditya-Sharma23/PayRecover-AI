import { useEffect, useState } from "react";
import { apiJson, getToken, setToken } from "./api/client.js";
import HistoryPanel from "./components/HistoryPanel.jsx";
import PaymentPanel from "./components/PaymentPanel.jsx";
import ReportView from "./components/ReportView.jsx";

const METRICS = [
  ["total_runs", "Total runs", "▤", "Saved"],
  ["vulnerabilities", "Vulnerabilities", "⚠", "Potential"],
  ["security_issues", "Security issues", "⬡", "Review"],
  ["accessibility_issues", "Accessibility issues", "♿", "WCAG"],
];

export default function App() {
  const [serviceState, setServiceState] = useState("Connecting");
  const [aiState, setAiState] = useState("AI status unknown");
  const [aiName, setAiName] = useState("AI");
  const [result, setResult] = useState(null);
  const [historyKey, setHistoryKey] = useState(0);
  const [token, setTokenState] = useState(getToken());
  const [needsToken, setNeedsToken] = useState(false);
  const [stats, setStats] = useState(null);

  useEffect(() => {
    apiJson("/api/health")
      .then((data) => setServiceState(data.status === "ok" ? "API connected" : "API unavailable"))
      .catch(() => setServiceState("API offline"));
    apiJson("/api/ai/status")
      .then((data) => {
        const name = data.name || "AI";
        setAiName(name);
        setAiState(data.configured ? `${name} ready (${data.model})` : `${name} off · built-in summaries`);
      })
      .catch(() => setAiState("AI status unavailable"));
    apiJson("/api/me")
      .then(() => setNeedsToken(false))
      .catch((error) => setNeedsToken(/authentication required/.test(error.message)));
  }, [token]);

  useEffect(() => {
    apiJson("/api/dashboard").then(setStats).catch(() => setStats(null));
  }, [historyKey, token]);

  function saveToken(value) {
    setToken(value.trim());
    setTokenState(value.trim());
    setHistoryKey((current) => current + 1);
  }

  function openReport(report) {
    setResult(report);
    setTimeout(() => document.getElementById("report")?.scrollIntoView({ behavior: "smooth" }), 50);
  }

  const online = serviceState === "API connected";
  return <div className="app-shell">
    <aside className="sidebar" aria-label="Main navigation">
      <a className="brand" href="#dashboard"><span className="brand-mark" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M3 22.5 45 3 29 45l-7-15.2L3 22.5Zm19.2 7.3L45 3 18 25.2" /></svg></span><span>Pay<span className="brand-accent">Recover</span> AI</span></a>
      <nav className="side-nav">
        <a href="#dashboard"><span aria-hidden="true">⌂</span> Dashboard</a>
        <a href="#payments"><span aria-hidden="true">$</span> Investigate order</a>
        <a href="#report"><span aria-hidden="true">◉</span> Latest report</a>
        <a href="#findings"><span aria-hidden="true">⚠</span> Vulnerabilities</a>
        <a href="#history"><span aria-hidden="true">▤</span> Run history</a>
      </nav>
      <div className="sidebar-note">
        <span className="shield" aria-hidden="true">⬡</span>
        <strong>Evidence first</strong>
        <p>PayPal&apos;s own record and real browser checks decide every result. {aiName} explains; it never changes a verdict.</p>
      </div>
    </aside>
    <main className="main-content" id="dashboard">
      <section className="welcome">
        <div><p className="eyebrow">PayRecover AI · PayPal Sandbox</p><h1>Payment &amp; Security Overview</h1><p>Find money a store has lost on PayPal, prove each leak, and check the store for the hole behind it.</p></div>
        <div className="date-label">
          <span className={`status-pill ${online ? "online" : ""}`}><i />{serviceState}</span>
          <span className="status-pill"><i className="ai" />{aiState}</span>
        </div>
      </section>

      {needsToken && <p className="notice notice-error" role="status">This server needs an API token. Paste it under <strong>Settings</strong> at the bottom of the page.</p>}

      <section className="metrics" aria-label="Run statistics">
        {METRICS.map(([key, title, icon, trend]) => <article className="metric-card" key={key}>
          <span className="metric-icon" aria-hidden="true">{icon}</span>
          <div><p>{title}</p><strong>{stats ? stats[key] : "–"}</strong></div>
          <span className="metric-trend">{trend}</span>
        </article>)}
      </section>

      <PaymentPanel onWebsiteReport={(report) => { setResult(report); setHistoryKey((value) => value + 1); }} onOpenReport={openReport} />

      {result ? <ReportView key={result.run_id} report={result} /> : <section className="panel" id="report"><div className="section-heading"><div><p className="eyebrow">Latest report</p><h2>No report yet</h2></div></div><p className="empty-state">Investigate an order to scan its store, or open a run from Run history, to see results grouped by HTTP response, content, security, accessibility and API contract.</p></section>}

      <HistoryPanel refreshKey={`${historyKey}-${token}`} onOpen={openReport} />

      <details className="panel token-settings" id="settings" open={needsToken || undefined}>
        <summary>{needsToken ? "Settings · this server needs an API token" : "Settings · API token (only when backend authentication is enabled)"}</summary>
        <input type="password" className="text-input" autoComplete="off" aria-label="API token" placeholder="Paste your PayRecover API token" defaultValue={token} onBlur={(event) => saveToken(event.target.value)} />
        <p className="hint">Kept in this browser tab&apos;s session storage only.</p>
      </details>
      <footer>PayRecover AI · PayPal Sandbox · Playwright + axe · security and XSS probes · Schemathesis · {aiName}</footer>
    </main>
  </div>;
}
