import assert from "node:assert/strict";
import { test } from "node:test";
import { MAX_HISTORY, agentBody, alertLabel, alertSettingsBody, disputeBody, disputeQuestion, disputeReason, proposalRequest, timeAgo } from "../src/lib/assist.js";

test("a dispute answer is always sent as approved, with the note trimmed", () => {
  assert.deepEqual(disputeBody("accept_claim", "  We agree.  "), { action: "accept_claim", note: "We agree.", approved: true });
});

test("accepting a claim warns that PayPal refunds the buyer", () => {
  const dispute = { dispute_id: "PP-D-1", amount: "18.00", currency: "USD" };
  assert.match(disputeQuestion(dispute, "accept_claim"), /refund 18\.00 USD/);
  assert.match(disputeQuestion(dispute, "provide_evidence"), /evidence/);
});

test("dispute reasons read as words", () => {
  assert.equal(disputeReason("DUPLICATE_TRANSACTION"), "Charged twice");
  assert.equal(disputeReason("SOMETHING_NEW"), "something new");
});

test("the assistant gets the newest messages only", () => {
  const history = Array.from({ length: 25 }, (_, index) => ({ role: "user", text: `q${index}`, tool_calls: [] }));
  const body = agentBody(history, "vulnerable");
  assert.equal(body.messages.length, MAX_HISTORY);
  assert.deepEqual(body.messages.at(-1), { role: "user", text: "q24" });
  assert.equal(body.store, "vulnerable");
});

test("approving a proposal sends the same approved calls as the dashboard", () => {
  assert.deepEqual(proposalRequest({ kind: "recovery", order_id: "5O190127TN364715T", store: "main", leak_kind: "never_captured" }), {
    path: "/api/recoveries", body: { order_id: "5O190127TN364715T", store: "main", kind: "never_captured", approved: true },
  });
  assert.deepEqual(proposalRequest({ kind: "dispute", dispute_id: "PP-D-1", dispute_action: "accept_claim", note: "We agree." }), {
    path: "/api/disputes/PP-D-1/respond", body: { action: "accept_claim", note: "We agree.", approved: true },
  });
});

test("alert settings send empty fields as null", () => {
  assert.deepEqual(alertSettingsBody(" me@shop.example ", "  "), { email: "me@shop.example", zapier_url: null });
});

test("alerts are labelled, with unknown kinds kept readable", () => {
  assert.deepEqual(alertLabel("leak_found"), ["Leak found", "failed"]);
  assert.deepEqual(alertLabel("new_kind"), ["new kind", "skipped"]);
});

test("alert times read as minutes, hours or days ago", () => {
  const now = Date.parse("2026-11-02T12:00:00Z");
  assert.equal(timeAgo("2026-11-02T11:59:30Z", now), "just now");
  assert.equal(timeAgo("2026-11-02T11:55:00Z", now), "5 min ago");
  assert.equal(timeAgo("2026-11-02T09:00:00Z", now), "3 h ago");
  assert.equal(timeAgo("2026-10-31T12:00:00Z", now), "2 d ago");
});
