import assert from "node:assert/strict";
import test from "node:test";
import { connectorBody, diffLines, fixBody, fixTitle, mappingRows } from "../src/lib/fixes.js";

test("a demo store's fix sends no code; a connected store's sends the pasted code", () => {
  assert.deepEqual(fixBody("xss", "vulnerable", "XSS found."), { issue: "xss", store: "vulnerable", detail: "XSS found." });
  assert.deepEqual(fixBody("xss", "connected", null, "def page(q): return q"),
    { issue: "xss", store: "connected", code: "def page(q): return q", filename: "store_code.py" });
});

test("a diff is split into coloured lines", () => {
  const lines = diffLines("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n same\n");
  assert.deepEqual(lines.map(([kind]) => kind), ["file", "file", "hunk", "del", "add", "same"]);
});

test("the connector form leaves out an empty products path and can carry an edited mapping", () => {
  const form = { name: " ", base_url: " http://shop ", orders_path: "/orders", products_path: " " };
  assert.deepEqual(connectorBody(form), { name: "Your store", base_url: "http://shop", orders_path: "/orders" });
  assert.equal(connectorBody(form, { a: 1 }).mapping.a, 1);
});

test("a mapping reads as plain rows", () => {
  const rows = mappingRows({ orders_list: "data.sales", money: "cents", paid_statuses: ["complete"],
    order_fields: { order_id: "payment.orderRef", status: "state", total: "amount.totalCents" }, product_fields: null });
  assert.deepEqual(rows[0], ["Orders list", "data.sales"]);
  assert.ok(rows.some(([label, value]) => label === "Money is counted in" && value.startsWith("cents")));
  assert.ok(rows.some(([label, value]) => label === "PayPal order id" && value === "payment.orderRef"));
});

test("the fix dialog is titled with the finding's first sentence", () => {
  assert.equal(fixTitle("xss", "The store page runs code a visitor sends (XSS). XSS found: 14 of 15."), "The store page runs code a visitor sends (XSS)");
  assert.equal(fixTitle("security_headers", null), "security headers");
});

