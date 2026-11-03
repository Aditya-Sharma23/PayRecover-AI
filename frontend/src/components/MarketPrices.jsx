import { useState } from "react";
import { apiJson } from "../api/client.js";
import { PRICE_VERDICTS } from "../lib/assist.js";

// Market price check (Channel3): each product's price next to what comparable products sell for elsewhere.
export default function MarketPrices({ store }) {
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function check() {
    setBusy(true);
    setError(null);
    try {
      setReport(await apiJson(`/api/stores/${encodeURIComponent(store)}/prices`, {}, "The price check could not be completed."));
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(false); }
  }

  const flagged = report?.items.filter((item) => item.verdict === "underpriced" || item.verdict === "overpriced") || [];
  return <div className="market-prices">
    <div className="check-row-head">
      <div><strong>Market price check</strong> <span className="count-chip">Channel3 · 25,000+ retailers</span>
        <p className="hint">Catches price typos that PayPal&apos;s records cannot show, by comparing each product with the market.</p></div>
      <button className="secondary-button" type="button" onClick={check} disabled={busy}>{busy ? "Asking Channel3…" : "Check prices against the market"}</button>
    </div>
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    {report?.note && <p className="notice" role="status">{report.note}</p>}
    {report?.checked && <p className={flagged.length ? "price-summary bad" : "price-summary"}>
      {flagged.length ? `${flagged.length} price${flagged.length === 1 ? "" : "s"} look wrong.` : "Every price is in line with the market."}</p>}
    {report?.items.length > 0 && <ul className="price-list">{report.items.map((item) => {
      const [label, tone] = PRICE_VERDICTS[item.verdict] || PRICE_VERDICTS.no_match;
      return <li key={item.product_id}>
        <span className={`badge ${tone}`}>{label}</span>
        <div>
          <strong>{item.name}</strong> · {item.store_price} USD{item.market_median && <> · market about {item.market_median} USD ({item.market_low}–{item.market_high}, {item.samples} products)</>}
          <small>{item.message}</small>
          {item.examples.length > 0 && <small>e.g. {item.examples.map((example, index) => <span key={example.url}>{index > 0 && ", "}
            <a href={example.url} target="_blank" rel="noreferrer noopener">{example.retailer}</a> {example.price}</span>)}</small>}
        </div>
      </li>;
    })}</ul>}
  </div>;
}
