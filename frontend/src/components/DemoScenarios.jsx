import { useEffect, useState } from "react";
import { apiJson } from "../api/client.js";

// Sample orders that show each result through the real rules, for a demo without a store or a working Sandbox.
export default function DemoScenarios({ onResult }) {
  const [scenarios, setScenarios] = useState([]);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    apiJson("/api/demo/scenarios").then(setScenarios).catch(() => setScenarios([]));
  }, []);

  async function run(id) {
    setBusy(id);
    setError(null);
    try {
      onResult(await apiJson(`/api/demo/scenarios/${encodeURIComponent(id)}`, {}, "The sample could not be loaded."));
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(null); }
  }

  if (!scenarios.length) return null;
  return <div className="demo-scenarios">
    <p className="hint"><strong>Try a sample:</strong> made-up orders through the same checks. Nothing at PayPal is touched.</p>
    <div className="demo-buttons">{scenarios.map((scenario) => <button key={scenario.id} type="button" className="secondary-button"
      title={scenario.description} disabled={Boolean(busy)} onClick={() => run(scenario.id)}>
      {busy === scenario.id ? "Loading…" : scenario.title}
    </button>)}</div>
    {error && <p className="notice notice-error" role="alert">{error}</p>}
  </div>;
}
