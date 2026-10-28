import { useEffect, useState } from "react";
import { apiJson, openHtmlPage } from "../api/client.js";
import { label } from "../lib/report.js";

const CHANGE_LABELS = {
  fixed: "Fixed", regressed: "Regressed", changed: "Changed", unchanged: "Unchanged", added: "Added", removed: "Removed",
};

function when(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function compactUrl(url) {
  try { const parsed = new URL(url); return parsed.host + parsed.pathname; } catch { return url; }
}

function Comparison({ comparison, onClose }) {
  return <section className="comparison" aria-label="Run comparison">
    <div className="comparison-head">
      <strong>{comparison.base_status} → {comparison.head_status}</strong>
      <span>{comparison.regressions} regression(s), {comparison.fixes} fix(es){comparison.same_target ? "" : " · different targets"}</span>
      <button type="button" className="history-view" onClick={onClose}>Close</button>
    </div>
    <div className="table-wrap"><table>
      <thead><tr><th>Check</th><th>Older run</th><th>Newer run</th><th>Change</th></tr></thead>
      <tbody>{comparison.changes.map((change) => <tr key={change.check_id} className={`change-${change.change}`}>
        <td>{label(change.check_id)}</td><td>{change.base_status || "—"}</td><td>{change.head_status || "—"}</td><td>{CHANGE_LABELS[change.change]}</td>
      </tr>)}</tbody>
    </table></div>
    <p className="hint">Compared from stored evidence only; nothing was re-run.</p>
  </section>;
}

export default function HistoryPanel({ refreshKey, onOpen }) {
  const [runs, setRuns] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [selected, setSelected] = useState([]);
  const [comparison, setComparison] = useState(null);
  const [error, setError] = useState(null);
  const [reload, setReload] = useState(0);
  const [me, setMe] = useState(null);

  useEffect(() => {
    apiJson("/api/runs?limit=20").then(setRuns).catch(() => setRuns([]));
    apiJson("/api/jobs?limit=5").then(setJobs).catch(() => setJobs([]));
    apiJson("/api/me").then(setMe).catch(() => setMe(null));
  }, [refreshKey, reload]);

  const act = async (work) => {
    setError(null);
    try { await work(); } catch (reason) { setError(reason.message); }
  };
  const toggle = (runId) => setSelected((current) => current.includes(runId)
    ? current.filter((id) => id !== runId) : [...current.slice(-1), runId]);

  async function compare() {
    // Order by time so "older → newer" reads naturally.
    const ordered = runs.filter((run) => selected.includes(run.run_id)).reverse();
    setComparison(await apiJson(`/api/runs/compare?base=${ordered[0].run_id}&head=${ordered[1].run_id}`));
  }

  async function remove(runId) {
    await apiJson(`/api/runs/${runId}`, { method: "DELETE" });
    setSelected((current) => current.filter((id) => id !== runId));
    setReload((value) => value + 1);
  }

  async function clearAll() {
    if (!window.confirm("Delete every stored run and screenshot in your history?")) return;
    await apiJson("/api/runs", { method: "DELETE" });
    setSelected([]);
    setComparison(null);
    setReload((value) => value + 1);
  }

  return <section className="panel history-panel" id="history" aria-labelledby="history-heading">
    <div className="section-heading">
      <div><p className="eyebrow">Saved locally</p><h2 id="history-heading">Run history</h2></div>
      <div className="button-row">
        <button type="button" className="secondary-button" disabled={selected.length !== 2} onClick={() => act(compare)}>Compare selected (2)</button>
        <button type="button" className="secondary-button danger" disabled={runs.length === 0} onClick={() => act(clearAll)}>Clear history</button>
      </div>
    </div>
    {me && <p className="hint">{me.auth_enabled ? `Signed in as ${me.owner}. ` : ""}Runs are kept for {me.retention_days} days (newest {me.max_runs_per_owner}). Tick two runs to compare them.</p>}
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {comparison && <Comparison comparison={comparison} onClose={() => setComparison(null)} />}
    {jobs.length > 0 && <div className="job-list" aria-label="Background jobs">
      {jobs.map((job) => <span className={`badge job-${job.status}`} key={job.job_id} title={job.error || ""}>{job.job_type} · {job.status}{job.target_url ? ` · ${compactUrl(job.target_url)}` : ""}</span>)}
    </div>}
    <div className="table-wrap" tabIndex={0} role="region" aria-label="Run history table">
      <table>
        <thead><tr><th><span className="sr-only">Compare</span></th><th>URL</th><th>Checks</th><th>Date &amp; time</th><th>Results</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
          {runs.length === 0 ? <tr><td colSpan={7} className="empty-state">No stored runs yet.</td></tr> : runs.map((run) => <tr key={run.run_id}>
            <td><input type="checkbox" className="row-select" aria-label={`Select run ${run.target_url} for comparison`} checked={selected.includes(run.run_id)} onChange={() => toggle(run.run_id)} /></td>
            <td className="url-cell" title={run.target_url}>{compactUrl(run.target_url)}</td>
            <td title={(run.checks || []).map(label).join(", ")}>{(run.checks || []).length}</td>
            <td>{when(run.finished_at)}</td>
            <td>{run.summary.passed} pass · {run.summary.failed} fail</td>
            <td><span className={`badge ${run.overall_status}`}>{run.overall_status}</span></td>
            <td className="row-actions">
              <button type="button" className="history-view" onClick={() => act(async () => onOpen(await apiJson(`/api/runs/${run.run_id}`)))}>View</button>
              <button type="button" className="history-view" onClick={() => act(() => openHtmlPage(`/api/runs/${run.run_id}/html`))}>HTML</button>
              <button type="button" className="history-view danger" aria-label={`Delete run ${run.target_url}`} onClick={() => act(() => remove(run.run_id))}>Delete</button>
            </td>
          </tr>)}
        </tbody>
      </table>
    </div>
  </section>;
}
