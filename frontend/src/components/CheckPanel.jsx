import { useState } from "react";

const TABS = [
  ["store", "Whole store"],
  ["order", "One order (deep check)"],
];

// Check payments: the store scan and the one-order investigation, as two tabs of one panel.
// Both stay mounted, so switching tabs keeps each one's results.
export default function CheckPanel({ store, order }) {
  const [tab, setTab] = useState("store");
  return <section className="panel" id="check" aria-labelledby="check-heading">
    <div className="section-heading">
      <div><p className="eyebrow">PayRecover · PayPal Sandbox</p><h2 id="check-heading">Check payments</h2></div>
    </div>
    <div className="tabs" role="tablist" aria-label="What to check">
      {TABS.map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={tab === key}
        className={tab === key ? "selected" : ""} onClick={() => setTab(key)}>{label}</button>)}
    </div>
    <div role="tabpanel" hidden={tab !== "store"}>{store}</div>
    <div role="tabpanel" hidden={tab !== "order"}>{order}</div>
  </section>;
}
