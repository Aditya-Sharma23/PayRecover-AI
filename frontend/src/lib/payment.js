// Pure helpers for the PayPal Sandbox verification panel.

// What the tester expects PayPal to show, mapped to the verify request fields.
export const OUTCOMES = [
  ["paid", "Paid and captured", { expected_status: "COMPLETED", require_capture: true }],
  ["approved", "Approved, not captured", { expected_status: "APPROVED", require_capture: false }],
  ["created", "Not paid / cancelled", { expected_status: "CREATED", require_capture: false }],
];

export const PAYMENT_CHECK_LABELS = {
  order_found: "Order exists at PayPal",
  order_status: "Order status",
  capture_completed: "Payment captured",
  amount_matches: "Amount and currency",
};

const ORDER_ID = /^[A-Z0-9]{8,40}$/;
const AMOUNT = /^\d{1,10}(\.\d{1,2})?$/;
const CURRENCY = /^[A-Z]{3}$/;

export const emptyPaymentForm = () => ({ orderId: "", expectedAmount: "", currency: "USD", outcome: "paid" });

export function paymentProblems(form) {
  const problems = [];
  const orderId = form.orderId.trim().toUpperCase();
  if (orderId && !ORDER_ID.test(orderId)) problems.push("Order id must be 8–40 letters and digits, as shown by the store.");
  if (form.expectedAmount.trim() && !AMOUNT.test(form.expectedAmount.trim())) problems.push("Expected amount must look like 18.00.");
  if (!CURRENCY.test(form.currency.trim().toUpperCase())) problems.push("Currency must be a 3-letter code such as USD.");
  return problems;
}

export function paymentBody(form) {
  const outcome = OUTCOMES.find(([key]) => key === form.outcome) || OUTCOMES[0];
  const amount = form.expectedAmount.trim();
  return {
    order_id: form.orderId.trim().toUpperCase(),
    ...outcome[2],
    ...(amount ? { expected_amount: amount } : {}),
    expected_currency: form.currency.trim().toUpperCase(),
  };
}
