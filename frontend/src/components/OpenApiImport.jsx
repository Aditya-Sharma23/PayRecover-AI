import { useState } from "react";
import { apiJson } from "../api/client.js";

const MAX_SPEC_BYTES = 500_000;

export default function OpenApiImport({ onUse }) {
  const [spec, setSpec] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function loadFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > MAX_SPEC_BYTES) {
      setError("The file is larger than 500 KB.");
      return;
    }
    setSpec(await file.text());
    setError(null);
  }

  async function importSpec() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await apiJson("/api/openapi/import", {
        method: "POST",
        body: { spec, base_url: baseUrl.trim() || null },
      }, "The OpenAPI document could not be imported."));
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(false); }
  }

  return <details className="import-panel">
    <summary>Import checks from an OpenAPI / Swagger document</summary>
    <p className="fine-print">Paste JSON or YAML, or choose a file. Nothing is run: each GET operation becomes a suggestion you review before running.</p>
    <textarea aria-label="OpenAPI document" rows={6} placeholder='{"openapi": "3.0.0", ...}' value={spec} onChange={(event) => setSpec(event.target.value)} />
    <div className="editor-row">
      <input type="file" accept=".json,.yaml,.yml,application/json" aria-label="OpenAPI file" onChange={loadFile} />
      <input aria-label="Base URL override" placeholder="Base URL override, e.g. https://api.example.com/v1" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
    </div>
    <button type="button" className="secondary-button" disabled={busy || spec.trim().length < 2} onClick={importSpec}>{busy ? "Reading document..." : "Import operations"}</button>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {result && <div className="operation-list">
      <p><strong>{result.title}</strong> <small>v{result.version}{result.base_url ? ` · ${result.base_url}` : ""}</small></p>
      {result.warnings.map((warning) => <p className="fine-print" key={warning}>{warning}</p>)}
      {result.operations.map((operation) => <div className="operation" key={`${operation.method} ${operation.path}`}>
        <span className={`method method-${operation.method.toLowerCase()}`}>{operation.method}</span>
        <span className="operation-text"><code>{operation.path}</code>{operation.summary && <small>{operation.summary}</small>}{operation.reason && <small className="operation-reason">{operation.reason}</small>}</span>
        <button type="button" className="text-button" disabled={operation.method !== "GET" || !operation.url} onClick={() => onUse(operation)}>{operation.runnable ? "Use" : "Use & edit"}</button>
      </div>)}
    </div>}
  </details>;
}
