// Pure helpers for the PayRecover order investigation panel.

export const CHECK_GROUPS = [
  ["Store's claim", ["store_claim_found", "store_claim_matches", "price_integrity"]],
  ["PayPal's record", ["order_found", "order_status", "capture_completed", "payment_hold", "amount_matches", "single_capture", "no_duplicate_charge"]],
  ["Security probes", ["price_tampering_probe"]],
];

export const CHECK_LABELS = {
  store_claim_found: "Store has this order",
  store_claim_matches: "Store's claim matches PayPal",
  price_integrity: "Prices match the catalog",
  order_found: "Order exists at PayPal",
  order_status: "Order status",
  capture_completed: "Payment captured",
  payment_hold: "Held payment collected",
  amount_matches: "Amount and currency",
  single_capture: "Captured only once",
  no_duplicate_charge: "Cart charged only once",
  price_tampering_probe: "Price tampering probe",
};

export const VERDICTS = {
  healthy: ["Healthy", "passed", "The store's claim matches PayPal's record."],
  leaking: ["Money leak found", "failed", "The store and PayPal disagree, and money is at risk."],
  review: ["Needs review", "warning", "Some checks could not be completed."],
  waiting: ["Waiting for PayPal", "waiting", "No leak found, but PayPal has not settled this payment yet."],
};

export const RECOVERY_LABELS = {
  capture: "Capture payment", capture_hold: "Capture held payment", invoice: "Send invoice", refund: "Refund extra charge",
  hold_shipment: "Hold shipment",
};

// Recovery outcomes: label and badge class.
export const RECOVERY_STATUS = {
  recovered: ["Recovered", "passed"],
  invoice_sent: ["Invoice sent", "passed"],
  refunded: ["Refunded", "passed"],
  unverified: ["Not confirmed", "warning"],
  failed: ["Failed", "failed"],
};

// What the merchant is asked before any money moves.
export function approvalQuestion(leak, orderId) {
  const verb = { capture: "Capture", capture_hold: "Capture the held", invoice: "Invoice the buyer for", refund: "Refund" }[leak.recovery_action];
  return `${verb} ${leak.amount} ${leak.currency} for order ${orderId} in PayPal Sandbox?\n\n${leak.recovery}`;
}

export const recoveryBody = (orderId, store, kind) => ({ order_id: orderId, store, kind, approved: true });

const LEAK_NAMES = {
  never_captured: "Never captured", fake_paid: "Fake paid", wrong_amount: "Wrong amount", duplicate_charge: "Duplicate charge",
  hold_expiring: "Held, not collected", hold_expired: "Hold expired",
};

// Grid rows for the recovery table; "when" sorts correctly as text.
export function recoveryRows(records) {
  return records.map((record) => ({
    id: record.recovery_id,
    when: record.created_at.slice(0, 16).replace("T", " "),
    order_id: record.order_id,
    store: STORE_SHORT_NAMES[record.store] || "Main",
    leak: LEAK_NAMES[record.kind] || record.kind,
    action: record.action,
    amount: `${record.amount} ${record.currency}`,
    status: record.status,
    message: record.message,
  }));
}

const ORDER_ID = /^[A-Z0-9]{8,40}$/;

export const normalizeOrderId = (value) => value.trim().toUpperCase();

export function orderIdProblem(value) {
  const orderId = normalizeOrderId(value);
  return orderId && !ORDER_ID.test(orderId) ? "Order id must be 8–40 letters and digits, as shown by the store." : null;
}

// The stores PayRecover can check. Their addresses are operator settings; the dashboard only picks one.
export const STORES = [
  ["main", "Main store (demo)"],
  ["vulnerable", "Vulnerable store (demo bugs)"],
  ["risky", "Risky store (safe payments, weak security)"],
  ["connected", "Your connected store"],
];

const STORE_SHORT_NAMES = { main: "Main", vulnerable: "Vulnerable", risky: "Risky", connected: "Yours" };

export const investigationBody = (value, { store = "main", probe = true, website = true, xss = true, explain = true } = {}) => ({
  order_id: normalizeOrderId(value),
  store,
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

// How soon to act on a leak: label and badge class.
export const URGENCY = {
  now: ["Act now", "failed"],
  soon: ["Soon", "warning"],
  normal: ["Normal", "skipped"],
};

// "Buyer approved 2h 14m ago" from PayPal's own timestamp; null when PayPal gave none.
export function waitingText(since, now = Date.now()) {
  if (!since) return null;
  const minutes = Math.max(0, Math.floor((now - new Date(since).getTime()) / 60000));
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const rest = minutes % 60;
  const span = days ? `${days}d ${hours}h` : hours ? `${hours}h ${rest}m` : `${rest}m`;
  return `Buyer approved ${span} ago, still not captured`;
}

// "Hold expires in 1d 4h" from PayPal's own expiration_time; "Hold expired" once it has passed.
export function expiresText(until, now = Date.now()) {
  if (!until) return null;
  const minutes = Math.floor((new Date(until).getTime() - now) / 60000);
  if (minutes <= 0) return "Hold expired: PayPal gave the money back";
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  return `Hold expires in ${days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`}`;
}

export const evidencePath = (orderId, store) => `/api/orders/${encodeURIComponent(orderId)}/evidence?store=${encodeURIComponent(store)}`;

export const scanBody = (store, limit = 25) => ({ store, limit, explain: true });

const DONE = new Set(["recovered", "invoice_sent", "refunded"]);

// What a scan still has at risk once some rows were approved and confirmed: money and orders.
export function remainingAtRisk(result, outcomes = {}) {
  let money = 0;
  let orders = 0;
  for (const item of result.items) {
    const open = item.leaks.filter((leak) => !DONE.has(outcomes[`${item.order_id}-${leak.kind}`]?.record?.status));
    money += open.reduce((sum, leak) => sum + Math.round(Number(leak.amount) * 100), 0);
    if (open.length) orders += 1;
  }
  return { money: (money / 100).toFixed(2), orders };
}

// One grid row per leak, in the scan's ranked order, with the plan's reason to act now beside it.
export function scanRows(result) {
  let rank = 0;
  const why = Object.fromEntries((result.plan?.steps || []).map((step) => [`${step.order_id}-${step.kind}`, step.why]));
  return result.items.flatMap((item) => item.leaks.map((leak) => ({
    id: `${item.order_id}-${leak.kind}`,
    rank: (rank += 1),
    order_id: item.order_id,
    leak,
    title: leak.title,
    amount: `${leak.amount} ${leak.currency}`,
    urgency: leak.urgency,
    why: why[`${item.order_id}-${leak.kind}`] || "",
    waiting_since: leak.waiting_since,
    expires_at: leak.expires_at,
    action: leak.recovery_action,
  })));
}

// Money recovered over time: one point per completed recovery, oldest first, with the running total.
// Failed or still-pending recoveries moved no money, so they are left out.
export function recoveredOverTime(records) {
  let total = 0;
  return records
    .filter((record) => DONE.has(record.status))
    .map((record) => ({ ...record, time: Date.parse(record.created_at) }))
    .filter((record) => Number.isFinite(record.time))
    .sort((a, b) => a.time - b.time)
    .map((record) => {
      total = Math.round((total + Number(record.amount)) * 100) / 100;
      return { time: record.time, amount: Number(record.amount), total, currency: record.currency,
        action: record.action, order_id: record.order_id };
    });
}
