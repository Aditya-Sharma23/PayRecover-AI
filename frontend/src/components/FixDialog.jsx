import { useRef, useState } from "react";
import { apiJson } from "../api/client.js";
import { diffLines, fixBody, fixTitle } from "../lib/fixes.js";

// "Write the fix": the AI writes the code change for one finding; PayRecover checks it and, for a demo store,
// runs the fixed copy to show the hole is closed. Nothing in any store is changed.
export default function FixButton({ issue, store, detail, label = "Write the fix" }) {
  const dialog = useRef(null);
  const [fix, setFix] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [code, setCode] = useState("");
  const demo = store !== "connected";

  async function write() {
    setBusy(true);
    setError(null);
    setFix(null);
    try {
      setFix(await apiJson("/api/fixes", { method: "POST", body: fixBody(issue, store, detail, demo ? null : code) },
        "The fix could not be written."));
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(false); }
  }

  function open() {
    dialog.current?.showModal();
    if (demo && !fix && !busy) write();
  }

  function download() {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([fix.diff], { type: "text/x-diff" }));
    link.download = `payrecover-fix-${issue}.diff`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  return <>
    <button className="secondary-button fix-button" type="button" onClick={open} title="The AI writes the code change; PayRecover checks it">{label}</button>
    <dialog ref={dialog} className="fix-dialog" aria-labelledby={`fix-${issue}`}>
      <div className="check-row-head">
        <h3 id={`fix-${issue}`}>Fix: {fixTitle(issue, detail)}</h3>
        <button className="secondary-button" type="button" onClick={() => dialog.current?.close()}>Close</button>
      </div>
      {!demo && !fix && <>
        <p className="hint">Paste the part of your store&apos;s code that handles this. The AI writes the change; PayRecover checks it, but cannot run your store&apos;s code.</p>
        <textarea rows={10} value={code} onChange={(event) => setCode(event.target.value)} placeholder="def checkout(...):" />
        <button className="danger-button" type="button" disabled={busy || code.trim().length < 20} onClick={write}>{busy ? "Writing…" : "Write the fix"}</button>
      </>}
      {busy && <p className="hint"><span className="spinner" aria-hidden="true" /> The AI is reading the store&apos;s code, then PayRecover runs the fixed copy…</p>}
      {error && <p className="notice notice-error" role="alert">{error}</p>}
      {fix && <div className="fix-result">
        <p className={`notice ${fix.verified ? "fix-verified" : ""}`} role="status">
          <span className={`badge ${fix.verified ? "passed" : "warning"}`}>{fix.verified ? "Fix verified" : "Not verified"}</span> {fix.summary}
        </p>
        <ul className="fix-checks">{fix.checks.map((check) => <li key={check.name}>
          <span className={`badge ${check.status}`}>{check.status === "passed" ? "✓" : check.status === "failed" ? "✗" : "–"}</span> {check.message}
        </li>)}</ul>
        <pre className="fix-diff" aria-label={`Changes to ${fix.filename}`}>{diffLines(fix.diff).map(([kind, text], index) =>
          <span key={index} className={`diff-${kind}`}>{text}{"\n"}</span>)}</pre>
        <div className="grid-actions">
          <button className="secondary-button" type="button" onClick={download}>Download .diff</button>
          <button className="secondary-button" type="button" onClick={write} disabled={busy}>Write it again</button>
          <small className="hint">Written by {fix.source}. PayRecover does not change your store: apply the diff yourself.</small>
        </div>
      </div>}
    </dialog>
  </>;
}
