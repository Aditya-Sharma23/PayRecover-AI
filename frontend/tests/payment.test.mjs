import assert from "node:assert/strict";
import { test } from "node:test";
import { groupChecks, investigationBody, orderIdProblem, sortPayloads } from "../src/lib/payment.js";

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
