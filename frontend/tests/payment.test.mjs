import assert from "node:assert/strict";
import { test } from "node:test";
import { approvalQuestion, evidencePath, expiresText, groupChecks, investigationBody, orderIdProblem, recoveryBody, recoveryRows, remainingAtRisk, scanRows, sortPayloads, waitingText, recoveredOverTime } from "../src/lib/payment.js";

test("the investigation body normalizes the order id", () => {
  assert.deepEqual(investigationBody(" 5o190127tn364715t "), {
    order_id: "5O190127TN364715T",
    store: "main",
    probe_store: true,
    website_checks: true,
    xss_probe: true,
    explain: true,
  });
  assert.equal(investigationBody("5O190127TN364715T", { xss: false }).xss_probe, false);
  assert.equal(investigationBody("5O190127TN364715T", { store: "vulnerable" }).store, "vulnerable");
  assert.equal(investigationBody("5O190127TN364715T", { probe: false }).probe_store, false);
  assert.equal(investigationBody("5O190127TN364715T", { website: false }).website_checks, false);
});

test("malformed order ids are reported before sending", () => {
  assert.equal(orderIdProblem("5O190127TN364715T"), null);
  assert.equal(orderIdProblem(""), null);
  assert.ok(orderIdProblem("../oauth2"));
});

test("checks are grouped by source, with unknown checks kept", () => {
  const groups = groupChecks([
    { name: "capture_completed" }, { name: "store_claim_found" }, { name: "price_tampering_probe" }, { name: "new_check" },
  ]);
  assert.deepEqual(groups.map(([title, items]) => [title, items.map((item) => item.name)]), [
    ["Store's claim", ["store_claim_found"]],
    ["PayPal's record", ["capture_completed"]],
    ["Security probes", ["price_tampering_probe"]],
    ["Other", ["new_check"]],
  ]);
});

test("unescaped XSS payloads are listed first", () => {
  const order = sortPayloads([
    { id: "a", outcome: "not_reflected" }, { id: "b", outcome: "encoded" }, { id: "c", outcome: "unescaped" },
  ]).map((item) => item.id);
  assert.deepEqual(order, ["c", "b", "a"]);
});

test("an approval names the action, amount and order, and sends no amount", () => {
  const leak = { recovery_action: "refund", amount: "18.00", currency: "USD", recovery: "Refund the extra charge." };
  assert.match(approvalQuestion(leak, "5O190127TN364715T"), /^Refund 18.00 USD for order 5O190127TN364715T in PayPal Sandbox\?/);
  assert.deepEqual(recoveryBody("5O190127TN364715T", "vulnerable", "duplicate_charge"), {
    order_id: "5O190127TN364715T", store: "vulnerable", kind: "duplicate_charge", approved: true,
  });
});

test("collecting a held payment is asked in plain words", () => {
  const leak = { recovery_action: "capture_hold", amount: "18.00", currency: "USD", recovery: "Capture it before 05 Nov." };
  assert.match(approvalQuestion(leak, "5O190127TN364715T"), /^Capture the held 18.00 USD for order 5O190127TN364715T/);
});

test("the hold clock counts down to PayPal's expiration time", () => {
  const until = "2026-11-05T12:00:00Z";
  assert.equal(expiresText(until, Date.parse("2026-11-03T08:00:00Z")), "Hold expires in 2d 4h");
  assert.equal(expiresText(until, Date.parse("2026-11-05T09:30:00Z")), "Hold expires in 2h 30m");
  assert.equal(expiresText(until, Date.parse("2026-11-05T13:00:00Z")), "Hold expired: PayPal gave the money back");
  assert.equal(expiresText(null), null);
});

test("recovery records become readable grid rows", () => {
  const [row] = recoveryRows([{
    recovery_id: "r1", created_at: "2026-10-31T09:15:42.1Z", order_id: "5O190127TN364715T", store: "vulnerable",
    kind: "duplicate_charge", action: "refund", amount: "18.00", currency: "USD", status: "refunded", message: "Refunded.",
  }]);
  assert.deepEqual(row, {
    id: "r1", when: "2026-10-31 09:15", order_id: "5O190127TN364715T", store: "Vulnerable", leak: "Duplicate charge",
    action: "refund", amount: "18.00 USD", status: "refunded", message: "Refunded.",
  });
});

test("the waiting clock counts from PayPal's timestamp", () => {
  const since = "2026-10-31T09:00:00Z";
  assert.equal(waitingText(since, Date.parse("2026-10-31T09:07:30Z")), "Buyer approved 7m ago, still not captured");
  assert.equal(waitingText(since, Date.parse("2026-10-31T11:14:00Z")), "Buyer approved 2h 14m ago, still not captured");
  assert.equal(waitingText(since, Date.parse("2026-11-02T12:00:00Z")), "Buyer approved 2d 3h ago, still not captured");
  assert.equal(waitingText(null), null);
});

test("scan rows keep the ranked order and number every leak", () => {
  const leak = (kind) => ({ kind, title: kind, amount: "1.00", currency: "USD", urgency: "now", recovery_action: "capture", targets: [{}] });
  const rows = scanRows({ items: [{ order_id: "A", leaks: [leak("never_captured")] }, { order_id: "B", leaks: [leak("fake_paid"), leak("wrong_amount")] }] });
  assert.deepEqual(rows.map((row) => [row.rank, row.order_id, row.leak.kind]), [[1, "A", "never_captured"], [2, "B", "fake_paid"], [3, "B", "wrong_amount"]]);
  assert.equal(evidencePath("A1B2", "vulnerable"), "/api/orders/A1B2/evidence?store=vulnerable");
});

test("approved rows leave the money still at risk", () => {
  const result = { items: [
    { order_id: "A", leaks: [{ kind: "never_captured", amount: "54.00" }] },
    { order_id: "B", leaks: [{ kind: "fake_paid", amount: "18.00" }] },
  ] };
  assert.deepEqual(remainingAtRisk(result), { money: "72.00", orders: 2 });
  assert.deepEqual(remainingAtRisk(result, { "A-never_captured": { record: { status: "recovered" } } }), { money: "18.00", orders: 1 });
  // A recovery PayPal did not confirm still counts as at risk.
  assert.deepEqual(remainingAtRisk(result, { "A-never_captured": { record: { status: "unverified" } } }), { money: "72.00", orders: 2 });
});

test("each grid row carries the plan's reason to act now", () => {
  const [row] = scanRows({ items: [{ order_id: "A", leaks: [{ kind: "never_captured", amount: "1.00" }] }],
    plan: { steps: [{ order_id: "A", kind: "never_captured", why: "The approval can lapse." }] } });
  assert.equal(row.why, "The approval can lapse.");
});

test("money recovered over time is a running total of completed recoveries, oldest first", () => {
  const points = recoveredOverTime([
    { status: "recovered", amount: "54.00", currency: "USD", action: "capture", order_id: "B", created_at: "2026-11-09T04:36:28Z" },
    { status: "failed", amount: "99.00", currency: "USD", action: "capture", order_id: "X", created_at: "2026-11-08T00:00:00Z" },
    { status: "refunded", amount: "18.10", currency: "USD", action: "refund", order_id: "A", created_at: "2026-11-01T10:00:00Z" },
  ]);
  assert.deepEqual(points.map((point) => [point.order_id, point.total]), [["A", 18.1], ["B", 72.1]]);
});
