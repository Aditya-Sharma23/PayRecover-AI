import { Suspense, lazy, useEffect, useState } from "react";
import { apiJson, getToken, setToken } from "./api/client.js";
import AlertsPanel from "./components/AlertsPanel.jsx";
import AssistantPanel from "./components/AssistantPanel.jsx";
import CheckPanel from "./components/CheckPanel.jsx";
import ConnectorPanel from "./components/ConnectorPanel.jsx";
import DisputesPanel from "./components/DisputesPanel.jsx";
import OnVisible from "./components/OnVisible.jsx";
import OverviewPanel from "./components/OverviewPanel.jsx";
import HistoryPanel from "./components/HistoryPanel.jsx";
import PaymentPanel from "./components/PaymentPanel.jsx";
import RecoveryPanel from "./components/RecoveryPanel.jsx";
import ScanPanel from "./components/ScanPanel.jsx";
import ReportView from "./components/ReportView.jsx";
import { PAGES, pageFor } from "./lib/pages.js";

// Bryntum Scheduler is large; load the timeline in its own chunk so the rest of the dashboard opens fast.
const TimelinePanel = lazy(() => import("./components/TimelinePanel.jsx"));

const METRICS = [
  ["total_runs", "Total runs", "▤", "Saved"],
  ["vulnerabilities", "Vulnerabilities", "⚠", "Potential"],
  ["security_issues", "Security issues", "⬡", "Review"],
  ["accessibility_issues", "Accessibility issues", "♿", "WCAG"],
];

const ALERTS_ICON = <svg className="nav-icon" viewBox="0 0 24 24"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15L6 16Z" /><path d="M10 20.5a2 2 0 0 0 4 0" /></svg>;
const ICONS = { dashboard: "⌂", check: "⌕", assistant: "✦", disputes: "⚖", timeline: "▦", alerts: ALERTS_ICON, recoveries: "↺", more: "▤" };

export default function App() {
  const [serviceState, setServiceState] = useState("Connecting");
  const [aiState, setAiState] = useState("AI status unknown");
  const [aiName, setAiName] = useState("AI");
  const [result, setResult] = useState(null);
  const [historyKey, setHistoryKey] = useState(0);
  const [recoveryKey, setRecoveryKey] = useState(0);
  const [token, setTokenState] = useState(getToken());
  const [needsToken, setNeedsToken] = useState(false);
  const [stats, setStats] = useState(null);
  const [overviewKey, setOverviewKey] = useState(0);
  const [page, setPage] = useState(() => pageFor(window.location.hash));

  // Each sidebar entry is its own page (#check, #disputes, ...). Every page stays mounted, so a scan's
  // results or a half-written chat survive switching pages; only the visible one is shown.
  useEffect(() => {
    const follow = () => {
      setPage(pageFor(window.location.hash));
      window.scrollTo(0, 0);
    };
    // hashchange covers links; popstate covers Back and Forward, and addresses changed by history.pushState.
    window.addEventListener("hashchange", follow);
    window.addEventListener("popstate", follow);
    return () => {
      window.removeEventListener("hashchange", follow);
      window.removeEventListener("popstate", follow);
    };
  }, []);

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
    window.location.hash = "#more";
  }

  const recovered = () => setRecoveryKey((value) => value + 1);
  const online = serviceState === "API connected";
  const shown = (key) => ({ className: "page", hidden: page !== key });
  return <div className="app-shell">
    <aside className="sidebar" aria-label="Main navigation">
      <a className="brand" href="#dashboard"><span className="brand-mark" aria-hidden="true"><svg viewBox="0 0 48 48"><path d="M3 22.5 45 3 29 45l-7-15.2L3 22.5Zm19.2 7.3L45 3 18 25.2" /></svg></span><span>Pay<span className="brand-accent">Recover</span> AI</span></a>
      <nav className="side-nav">
        {Object.entries(PAGES).map(([key, label]) => <a key={key} href={`#${key}`} className={page === key ? "active" : ""}
          aria-current={page === key ? "page" : undefined}><span aria-hidden="true">{ICONS[key]}</span> {label}</a>)}
      </nav>
      <div className="sidebar-note">
        <span className="shield" aria-hidden="true">⬡</span>
        <strong>Evidence first</strong>
        <p>PayPal&apos;s own record decides every result. {aiName} explains; it never changes a verdict.</p>
      </div>
    </aside>
    <main className="main-content" id="top">
      {/* The big title is the dashboard's; every other page opens with its own panel heading. */}
      <section className={`welcome ${page === "dashboard" ? "" : "compact"}`}>
        {page === "dashboard" && <div><p className="eyebrow">PayRecover AI · PayPal Sandbox</p><h1>Payment &amp; Security Overview</h1>
          <p>Find money a store has lost on PayPal, prove each leak, and get it back.</p></div>}
        <div className="date-label">
          <span className={`status-pill ${online ? "online" : ""}`}><i />{serviceState}</span>
          <span className="status-pill"><i className="ai" />{aiState}</span>
        </div>
      </section>

      {needsToken && <p className="notice notice-error" role="status">This server needs an API token. Paste it under <a href="#more">More tools → Settings</a>.</p>}

      <div {...shown("dashboard")}><OverviewPanel refreshKey={`${recoveryKey}-${overviewKey}-${token}`} /></div>

      <div {...shown("check")}>
        <CheckPanel
          store={<ScanPanel embedded onRecovered={recovered} onScanned={() => setOverviewKey((value) => value + 1)} />}
          order={<PaymentPanel embedded onWebsiteReport={(report) => { setResult(report); setHistoryKey((value) => value + 1); }} onOpenReport={openReport} onRecovered={recovered} />}
          connect={<ConnectorPanel onConnected={() => setOverviewKey((value) => value + 1)} />}
        />
      </div>

      <div {...shown("assistant")}><AssistantPanel onRecovered={recovered} /></div>

      <div {...shown("disputes")}><DisputesPanel onAnswered={recovered} /></div>

      <div {...shown("timeline")}>
        {/* Hidden pages are never on screen, so the timeline (and Bryntum) loads the first time this page opens. */}
        <OnVisible fallback={<section className="panel" id="timeline"><p className="hint">Loading the timeline…</p></section>}>
          <Suspense fallback={<section className="panel" id="timeline"><p className="hint">Loading the timeline…</p></section>}>
            <TimelinePanel refreshKey={`${recoveryKey}-${overviewKey}-${token}`} />
          </Suspense>
        </OnVisible>
      </div>

      <div {...shown("alerts")}><AlertsPanel refreshKey={`${recoveryKey}-${token}`} /></div>

      <div {...shown("recoveries")}><RecoveryPanel refreshKey={`${recoveryKey}-${token}`} /></div>

      <div {...shown("more")}>
        <section className="metrics" aria-label="Run statistics">
          {METRICS.map(([key, title, icon, trend]) => <article className="metric-card" key={key}>
            <span className="metric-icon" aria-hidden="true">{icon}</span>
            <div><p>{title}</p><strong>{stats ? stats[key] : "–"}</strong></div>
            <span className="metric-trend">{trend}</span>
          </article>)}
        </section>

        {result ? <ReportView key={result.run_id} report={result} /> : <section className="panel" id="report"><div className="section-heading"><div><p className="eyebrow">Website and API testing</p><h2>No report yet</h2></div></div><p className="empty-state">Check one order in depth, or open a run from Run history, to see its website report here.</p></section>}

        <HistoryPanel refreshKey={`${historyKey}-${token}`} onOpen={openReport} />

        <details className="panel token-settings" id="settings" open={needsToken || undefined}>
          <summary>{needsToken ? "Settings · this server needs an API token" : "Settings · API token (only when backend authentication is enabled)"}</summary>
          <input type="password" className="text-input" autoComplete="off" aria-label="API token" placeholder="Paste your PayRecover API token" defaultValue={token} onBlur={(event) => saveToken(event.target.value)} />
          <p className="hint">Kept in this browser tab&apos;s session storage only.</p>
        </details>
      </div>
      <footer>PayRecover AI · PayPal Sandbox · {aiName}</footer>
    </main>
  </div>;
}
