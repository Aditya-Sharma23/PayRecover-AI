// Vulnerable store checkout. Same flow as the Main store, plus demo controls for its bugs.
// Prices are editable and sent to the server; only the trust_client_price bug makes the server use them.

const state = { products: [], quantities: {}, prices: {}, cartId: "" };
const CART_KEY = "paytest-vuln-cart";
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

function saveCartId(cartId) {
  state.cartId = cartId;
  $("cart-id").textContent = cartId;
  try {
    localStorage.setItem(CART_KEY, cartId);
  } catch {
    // Storage can be blocked; the id still lasts for this page.
  }
}

function loadCartId() {
  try {
    return localStorage.getItem(CART_KEY) || crypto.randomUUID();
  } catch {
    return crypto.randomUUID();
  }
}

function cartLines() {
  return state.products
    .filter((product) => state.quantities[product.id] > 0)
    .map((product) => ({
      product_id: product.id,
      quantity: state.quantities[product.id],
      unit_price: Number(state.prices[product.id]).toFixed(2),
    }));
}

function renderCart() {
  const body = $("cart").querySelector("tbody");
  body.replaceChildren();
  let total = 0;
  for (const product of state.products) {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = product.name;
    const priceCell = document.createElement("td");
    const price = document.createElement("input");
    price.type = "number";
    price.min = "0.01";
    price.step = "0.01";
    price.value = state.prices[product.id];
    price.id = `price-${product.id}`;
    price.setAttribute("aria-label", `Price of ${product.name}`);
    price.addEventListener("change", () => {
      state.prices[product.id] = Math.max(0.01, Number(price.value) || 0.01);
      renderCart();
    });
    priceCell.append(price);
    const qtyCell = document.createElement("td");
    const select = document.createElement("select");
    select.setAttribute("aria-label", `Quantity of ${product.name}`);
    for (let n = 0; n <= 3; n += 1) select.add(new Option(String(n), String(n)));
    select.value = String(state.quantities[product.id]);
    select.addEventListener("change", () => {
      state.quantities[product.id] = Number(select.value);
      renderCart();
    });
    qtyCell.append(select);
    row.append(name, priceCell, qtyCell);
    body.append(row);
    total += Number(state.prices[product.id]) * state.quantities[product.id];
  }
  $("cart-total").textContent = total.toFixed(2);
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

async function renderBugs() {
  const bugs = await api("/api/bugs");
  const box = $("bugs");
  box.replaceChildren();
  for (const bug of bugs.available) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = bug.id;
    input.checked = bugs.active.includes(bug.id);
    input.addEventListener("change", async () => {
      const active = [...box.querySelectorAll("input:checked")].map((item) => item.value);
      await api("/api/bugs", { method: "POST", body: JSON.stringify({ bugs: active }) });
    });
    const text = document.createElement("span");
    text.textContent = bug.label;
    label.append(input, text);
    box.append(label);
  }
}

function loadSdk(clientId, currency) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    const params = new URLSearchParams({ "client-id": clientId, currency, intent: "capture" });
    script.src = `https://www.paypal.com/sdk/js?${params}`;
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
      }).catch((error) => {
        setStatus("error", error.message);
        throw error;
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
        // Unlike the Main store, the cart is kept after payment, so it can be paid again.
        if (order.status === "PAID") setStatus("paid", `Payment complete. Order ${order.order_id} is paid.`);
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
  $("new-cart").addEventListener("click", () => {
    saveCartId(crypto.randomUUID());
    setStatus("idle", "New cart started.");
  });
  try {
    await renderBugs();
    state.products = await api("/api/products");
    for (const product of state.products) {
      state.quantities[product.id] = product.id === "tee" ? 1 : 0;
      state.prices[product.id] = product.price;
    }
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
