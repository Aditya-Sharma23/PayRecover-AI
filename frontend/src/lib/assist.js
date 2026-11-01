// Disputes and the Ask PayRecover assistant: request bodies and labels.

export const DISPUTE_ACTIONS = {
  provide_evidence: ["Send evidence", "job-running"],
  accept_claim: ["Accept claim", "warning"],
  review: ["Review first", "skipped"],
};

export const DISPUTE_REASONS = {
  MERCHANDISE_OR_SERVICE_NOT_RECEIVED: "Item not received",
  MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED: "Not as described",
  UNAUTHORISED: "Unauthorised payment",
  CREDIT_NOT_PROCESSED: "Refund not processed",
  DUPLICATE_TRANSACTION: "Charged twice",
  INCORRECT_AMOUNT: "Wrong amount",
  PAYMENT_BY_OTHER_MEANS: "Paid another way",
  CANCELED_RECURRING_BILLING: "Cancelled subscription charged",
};

export const disputeReason = (reason) => DISPUTE_REASONS[reason] || (reason || "No reason given").replaceAll("_", " ").toLowerCase();

export const disputeBody = (action, note) => ({ action, note: note.trim(), approved: true });

export function disputeQuestion(dispute, action) {
  const amount = `${dispute.amount} ${dispute.currency}`;
  return action === "accept_claim"
    ? `Accept dispute ${dispute.dispute_id}? PayPal will refund ${amount} to the buyer and close the dispute.`
    : `Send this evidence to PayPal for dispute ${dispute.dispute_id} (${amount})?`;
}

export const MAX_HISTORY = 20;

// The conversation sent to the assistant: the newest messages, oldest first, in the API's shape.
export function agentBody(history, store) {
  return {
    store,
    messages: history.slice(-MAX_HISTORY).map(({ role, text }) => ({ role, text: text.slice(0, 4000) })),
  };
}

// What pressing Approve on an assistant proposal sends: the same approved calls as the dashboard.
export function proposalRequest(proposal) {
  if (proposal.kind === "dispute") {
    return {
      path: `/api/disputes/${encodeURIComponent(proposal.dispute_id)}/respond`,
      body: disputeBody(proposal.dispute_action, proposal.note || ""),
    };
  }
  return {
    path: "/api/recoveries",
    body: { order_id: proposal.order_id, store: proposal.store, kind: proposal.leak_kind, approved: true },
  };
}

export const EXAMPLE_QUESTIONS = [
  "How much money am I losing right now, and what should I fix first?",
  "Do I have any PayPal disputes? What should I answer?",
  "Did PayPal take any payments my store never recorded?",
  "Recover every urgent leak.",
];

// Alerts: where they go, and how each kind is labelled in the feed.
export const ALERT_EVENTS = {
  leak_found: ["Leak found", "failed"],
  scan_done: ["Scan", "job-running"],
  recovery_done: ["Recovered", "passed"],
  dispute_opened: ["Dispute", "warning"],
  unrecorded_payment: ["Not recorded", "failed"],
  test: ["Test", "skipped"],
};

export const alertLabel = (event) => ALERT_EVENTS[event] || [String(event || "alert").replaceAll("_", " "), "skipped"];

export const alertSettingsBody = (email, zapierUrl) => ({ email: email.trim() || null, zapier_url: zapierUrl.trim() || null });

export function timeAgo(when, now = Date.now()) {
  const seconds = Math.max(0, Math.round((now - new Date(when).getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`;
}
