// Main store checkout: the server prices the cart, creates and captures the order.
// Element ids and data-testid values are stable so PayRecover's browser checks can rely on them.

const state = { products: [], quantities: {}, cartId: "" };
const CART_KEY = "paytest-main-cart";

// One id per cart, kept across reloads; a paid cart is replaced by a new one.
function newCartId() {
  return crypto.randomUUID();
}

function loadCartId() {
  try {
    return localStorage.getItem(CART_KEY) || newCartId();
  } catch {
    return newCartId();
  }
}

function saveCartId(cartId) {
  state.cartId = cartId;
  try {
    localStorage.setItem(CART_KEY, cartId);
  } catch {
    // Private windows may block storage; the id still lasts for this page.
  }
}
// The page's CSP nonce. PayPal's SDK reads data-csp-nonce and adds it to the inline scripts it creates.
const cspNonce = document.currentScript?.nonce || "";
const $ = (id) => document.getElementById(id);

function setStatus(stateName, text) {
  const box = $("checkout-status");
  box.dataset.state = stateName;
  box.textContent = text;
}

function showOrder(order) {
  if (order.id || order.order_id) $("order-id").textContent = order.id || order.order_id;
  if (order.status) $("order-status").textContent = order.status;
}

function cartLines() {
  return state.products
    .filter((product) => state.quantities[product.id] > 0)
    // Only ids and quantities are sent: the server charges its own catalog price.
    .map((product) => ({ product_id: product.id, quantity: state.quantities[product.id] }));
}

// Simple line drawings for each product (24x24 paths), so the shop needs no image files.
const ART = {
  tee: ["M8 3 3 6l2 4 2-1v12h10V9l2 1 2-4-5-3c-.5 1.5-2 2.5-4 2.5S8.5 4.5 8 3Z"],
  mug: ["M5 6h11v9a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4V6Z", "M16 9h1.5a2.5 2.5 0 0 1 0 5H16"],
  stickers: ["m12 3 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.4l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9L12 3Z"],
};
const DEFAULT_ART = ["M5 8h14l-1.2 12H6.2L5 8Z", "M9 8V6a3 3 0 0 1 6 0v2"];

function productArt(productId) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const d of ART[productId] || DEFAULT_ART) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  const box = document.createElement("div");
  box.className = "product-art";
  box.append(svg);
  return box;
}

function renderCart() {
  const grid = $("cart");
  const lines = $("cart-lines");
  grid.replaceChildren();
  lines.replaceChildren();
  let total = 0;
  for (const product of state.products) {
    const quantity = state.quantities[product.id];
    const card = document.createElement("article");
    card.className = "product";
    card.dataset.product = product.id;
    card.dataset.price = product.price;
    card.dataset.selected = String(quantity > 0);
    const name = document.createElement("h3");
    name.textContent = product.name;
    const price = document.createElement("p");
    price.className = "price";
    price.textContent = `${product.price} USD`;
    const label = document.createElement("label");
    label.textContent = "Quantity";
    const select = document.createElement("select");
    select.id = `qty-${product.id}`;
    select.setAttribute("aria-label", `Quantity of ${product.name}`);
    for (let n = 0; n <= 3; n += 1) select.add(new Option(String(n), String(n)));
    select.value = String(quantity);
    select.addEventListener("change", () => {
      state.quantities[product.id] = Number(select.value);
      renderCart();
    });
    label.append(select);
    card.append(productArt(product.id), name, price, label);
    grid.append(card);
    if (quantity > 0) {
      const line = document.createElement("li");
      const what = document.createElement("span");
      what.textContent = `${quantity} × ${product.name}`;
      const cost = document.createElement("span");
      cost.textContent = (Number(product.price) * quantity).toFixed(2);
      line.append(what, cost);
      lines.append(line);
    }
    total += Number(product.price) * quantity;
  }
  if (!lines.children.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "Your cart is empty.";
    lines.append(empty);
  }
  $("cart-total").textContent = total.toFixed(2);
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function loadSdk(clientId, currency) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    const params = new URLSearchParams({ "client-id": clientId, currency, intent: "capture" });
    script.src = `https://www.paypal.com/sdk/js?${params}`;
    if (cspNonce) {
      script.nonce = cspNonce;
      script.dataset.cspNonce = cspNonce;
    }
    script.onload = resolve;
    script.onerror = () => reject(new Error("PayPal SDK failed to load"));
    document.head.append(script);
  });
}

function renderButtons() {
  window.paypal.Buttons({
    async createOrder() {
      const lines = cartLines();
      if (!lines.length) {
        setStatus("error", "Your cart is empty.");
        throw new Error("empty cart");
      }
      setStatus("creating", "Creating your order...");
      const order = await api("/api/orders", {
        method: "POST",
        body: JSON.stringify({ items: lines, cart_id: state.cartId }),
      });
      showOrder({ id: order.id, status: "PENDING" });
      setStatus("approving", `Order ${order.id} for ${order.total} ${order.currency}. Approve it in the PayPal window.`);
      return order.id;
    },
    async onApprove(data) {
      setStatus("capturing", "Capturing payment...");
      try {
        const order = await api(`/api/orders/${data.orderID}/capture`, { method: "POST" });
        showOrder(order);
        if (order.status === "PAID") {
          setStatus("paid", `Payment complete. Order ${order.order_id} is paid.`);
          saveCartId(newCartId());
        }
        else setStatus("error", `Payment not completed (${order.status}).`);
      } catch (error) {
        setStatus("error", `Payment failed: ${error.message}`);
      }
    },
    async onCancel(data) {
      const order = await api(`/api/orders/${data.orderID}/cancel`, { method: "POST" }).catch(() => null);
      if (order) showOrder(order);
      setStatus("cancelled", "Payment cancelled. You have not been charged.");
    },
    onError(error) {
      setStatus("error", `PayPal error: ${error.message || error}`);
    },
  }).render("#paypal-buttons");
}

async function start() {
  saveCartId(loadCartId());
  try {
    state.products = await api("/api/products");
    for (const product of state.products) state.quantities[product.id] = product.id === "tee" ? 1 : 0;
    renderCart();
    const config = await api("/api/config");
    $("currency").textContent = config.currency;
    await loadSdk(config.paypal_client_id, config.currency);
    renderButtons();
  } catch (error) {
    // Without Sandbox keys the shop still shows its products; only paying needs PayPal.
    setStatus("error", /PAYPAL_CLIENT_ID/.test(error.message)
      ? "PayPal is not set up for this store yet, so you can look around but not pay. Add your Sandbox keys to main-store/.env (see the README), then restart."
      : `The store could not start: ${error.message}`);
  }
}

start();
