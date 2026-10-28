import assert from "node:assert/strict";
import { test } from "node:test";
import { emptyPaymentForm, paymentBody, paymentProblems } from "../src/lib/payment.js";

const form = (change) => ({ ...emptyPaymentForm(), ...change });

test("a paid expectation requires a completed capture", () => {
  assert.deepEqual(paymentBody(form({ orderId: " 5o190127tn364715t ", expectedAmount: "18.00" })), {
    order_id: "5O190127TN364715T",
    expected_status: "COMPLETED",
    require_capture: true,
    expected_amount: "18.00",
    expected_currency: "USD",
  });
});

test("cancelled flows expect an uncaptured order and amount is optional", () => {
  const body = paymentBody(form({ orderId: "5O190127TN364715T", outcome: "created" }));
  assert.equal(body.expected_status, "CREATED");
  assert.equal(body.require_capture, false);
  assert.equal("expected_amount" in body, false);
});

test("malformed order ids, amounts and currencies are reported before sending", () => {
  assert.deepEqual(paymentProblems(form({ orderId: "5O190127TN364715T", expectedAmount: "18.00" })), []);
  assert.equal(paymentProblems(form({ orderId: "../oauth2" })).length, 1);
  assert.equal(paymentProblems(form({ expectedAmount: "1e3" })).length, 1);
  assert.equal(paymentProblems(form({ currency: "dollars" })).length, 1);
});
