// Pure helpers for the PayRecover order investigation panel.

export const CHECK_GROUPS = [
  ["Store's claim", ["store_claim_found", "store_claim_matches", "price_integrity"]],
  ["PayPal's record", ["order_found", "order_status", "capture_completed", "amount_matches", "single_capture"]],
  ["Security probes", ["price_tampering_probe"]],
];

export const CHECK_LABELS = {
  store_claim_found: "Store has this order",
  store_claim_matches: "Store's claim matches PayPal",
  price_integrity: "Prices match the catalog",
  order_found: "Order exists at PayPal",
  order_status: "Order status",
  capture_completed: "Payment captured",
  amount_matches: "Amount and currency",
  single_capture: "Charged only once",
  price_tampering_probe: "Price tampering probe",
};

export const VERDICTS = {
  healthy: ["Healthy", "passed", "The store's claim matches PayPal's record."],
  leaking: ["Money leak found", "failed", "The store and PayPal disagree, and money is at risk."],
  review: ["Needs review", "warning", "Some checks could not be completed."],
};

export const RECOVERY_LABELS = { capture: "Capture payment", invoice: "Send invoice", refund: "Refund extra charge" };

const ORDER_ID = /^[A-Z0-9]{8,40}$/;

export const normalizeOrderId = (value) => value.trim().toUpperCase();

export function orderIdProblem(value) {
  const orderId = normalizeOrderId(value);
  return orderId && !ORDER_ID.test(orderId) ? "Order id must be 8–40 letters and digits, as shown by the store." : null;
}

export const investigationBody = (value, { probe = true, website = true, xss = true, explain = true } = {}) => ({
  order_id: normalizeOrderId(value),
  probe_store: probe,
  website_checks: website,
  xss_probe: xss,
  explain,
});

// XSS probe: overall verdict and per-payload outcome labels, with the badge class each uses.
export const XSS_VERDICTS = {
  failed: ["XSS found", "failed"],
  warning: ["Raw in non-HTML", "warning"],
  passed: ["No XSS found", "passed"],
  error: ["Could not run", "error"],
};

export const XSS_OUTCOMES = {
  unescaped: ["Unescaped", "failed"],
  non_html: ["Raw, non-HTML", "warning"],
  encoded: ["Escaped", "passed"],
  not_reflected: ["Not reflected", "passed"],
  error: ["Error", "error"],
};

// Worst payloads first, so a finding is never below the fold.
export function sortPayloads(payloads) {
  const rank = { unescaped: 0, non_html: 1, error: 2, encoded: 3, not_reflected: 4 };
  return [...payloads].sort((a, b) => (rank[a.outcome] ?? 5) - (rank[b.outcome] ?? 5));
}

// Checks in display groups; any check the backend adds later lands in a final "Other" group.
export function groupChecks(checks) {
  const known = new Set(CHECK_GROUPS.flatMap(([, names]) => names));
  const groups = CHECK_GROUPS.map(([title, names]) => [title, checks.filter((check) => names.includes(check.name))]);
  const other = checks.filter((check) => !known.has(check.name));
  return [...groups, ["Other", other]].filter(([, items]) => items.length > 0);
}
