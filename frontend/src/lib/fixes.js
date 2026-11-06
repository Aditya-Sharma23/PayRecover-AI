// Helpers for the AI fix writer and the store connector.

export const FIXABLE = new Set(["xss", "security_headers", "price_tampering", "never_captured", "fake_paid",
  "wrong_amount", "duplicate_charge", "hold_expiring", "hold_expired"]);

export const fixBody = (issue, store, detail, code) => ({
  issue, store, ...(detail ? { detail: String(detail).slice(0, 1000) } : {}), ...(code ? { code, filename: "store_code.py" } : {}),
});

// The dialog's title: the finding's first sentence, or the issue's name.
export function fixTitle(issue, detail) {
  const first = String(detail || "").split(/(?<=\.)\s/)[0].replace(/\.$/, "");
  return first || issue.replaceAll("_", " ");
}

// A unified diff as [kind, line] pairs: "add", "del", "hunk", "file" or "same", for colouring.
export function diffLines(diff) {
  return String(diff || "").split("\n").filter((line, index, all) => line || index < all.length - 1).map((line) => {
    if (line.startsWith("+++") || line.startsWith("---")) return ["file", line];
    if (line.startsWith("@@")) return ["hunk", line];
    if (line.startsWith("+")) return ["add", line];
    if (line.startsWith("-")) return ["del", line];
    return ["same", line];
  });
}

// The store connector form, and the rows that explain a proposed mapping in plain words.
export const connectorBody = (form, mapping = null) => ({
  name: form.name.trim() || "Your store",
  base_url: form.base_url.trim(),
  orders_path: form.orders_path.trim(),
  ...(form.products_path.trim() ? { products_path: form.products_path.trim() } : {}),
  ...(mapping ? { mapping } : {}),
});

const FIELD_LABELS = [
  ["order_id", "PayPal order id"], ["status", "Status"], ["total", "Total"], ["currency", "Currency"],
  ["items", "Line items"], ["cart_id", "Cart"], ["capture_id", "PayPal capture id"], ["created_at", "Placed at"],
];

export function mappingRows(mapping) {
  const fields = mapping.order_fields;
  const rows = [["Orders list", mapping.orders_list || "the whole answer"]];
  for (const [key, label] of FIELD_LABELS) if (fields[key]) rows.push([label, fields[key]]);
  rows.push(["Paid when status is", mapping.paid_statuses.join(", ")]);
  rows.push(["Money is counted in", mapping.money === "cents" ? "cents (1800 = 18.00)" : "units (18.00)"]);
  if (mapping.product_fields) rows.push(["Product id and price", `${mapping.product_fields.id}, ${mapping.product_fields.price}`]);
  return rows;
}
