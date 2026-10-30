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

function renderCart() {
  const body = $("cart").querySelector("tbody");
  body.replaceChildren();
  let total = 0;
  for (const product of state.products) {
    const row = document.createElement("tr");
    row.dataset.product = product.id;
    row.dataset.price = product.price;
    const select = document.createElement("select");
    select.id = `qty-${product.id}`;
    select.setAttribute("aria-label", `Quantity of ${product.name}`);
    for (let n = 0; n <= 3; n += 1) select.add(new Option(String(n), String(n)));
    select.value = String(state.quantities[product.id]);
    select.addEventListener("change", () => {
      state.quantities[product.id] = Number(select.value);
      renderCart();
    });
    const cells = [product.name, product.price].map((text) => {
      const cell = document.createElement("td");
      cell.textContent = text;
      return cell;
    });
    const qtyCell = document.createElement("td");
    qtyCell.append(select);
    row.append(...cells, qtyCell);
    body.append(row);
    total += Number(product.price) * state.quantities[product.id];
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
    setStatus("error", `The store could not start: ${error.message}`);
  }
}

start();
