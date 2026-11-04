import { useEffect, useState } from "react";
import { apiJson } from "../api/client.js";

// Which PayPal Sandbox features work for these keys, asked of PayPal itself, with how to switch on the rest.
export default function PayPalSetup() {
  const [setup, setSetup] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    apiJson("/api/paypal/setup").then(setSetup).catch(() => setSetup(null));
  }, []);

  async function recheck() {
    setBusy(true);
    try {
      setSetup(await apiJson("/api/paypal/setup?refresh=true", {}, "PayPal setup could not be checked."));
    } catch {
      setSetup(null);
    } finally { setBusy(false); }
  }
  if (!setup) return null;

  const required = setup.items.filter((item) => item.required);
  const off = required.filter((item) => item.ok === false);
  const working = required.filter((item) => item.ok).length;
  const noKeys = setup.items[0]?.id === "credentials" && setup.items[0].ok === false;
  return <details className={`paypal-setup ${setup.ready ? "ready" : "not-ready"}`} open={off.length > 0}>
    <summary>
      <i aria-hidden="true" />
      {noKeys
        ? <span><strong>PayPal setup</strong> · <b>no PayPal keys yet</b>, so live checks are off. The sample buttons under Check payments still show every result.</span>
        : <span><strong>PayPal setup</strong> · {setup.ready ? "everything PayRecover needs is on" : `${working} of ${required.length} ready`}
          {off.length > 0 && <> · <b>{off.map((item) => item.name).join(", ")} {off.length === 1 ? "is" : "are"} off</b></>}</span>}
    </summary>
    <ul>{setup.items.map((item) => <li key={item.id}>
      <span className={`badge ${item.ok ? "passed" : item.ok === false ? "failed" : "skipped"}`}>
        {item.ok ? "On" : item.ok === false ? "Off" : item.required ? "Unknown" : "Optional"}
      </span>
      <div><strong>{item.name}</strong><small>{item.detail}</small>{item.fix && item.ok !== true && <small className="fix">Fix: {item.fix}</small>}</div>
    </li>)}</ul>
    <button className="secondary-button" type="button" disabled={busy} onClick={recheck}>{busy ? "Asking PayPal…" : "Check again"}</button>
  </details>;
}
