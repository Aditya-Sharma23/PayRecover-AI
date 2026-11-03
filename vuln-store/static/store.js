// Vulnerable store checkout. Same flow as the Main store, plus demo controls for its bugs.
// Prices are editable and sent to the server; only the trust_client_price bug makes the server use them.

const state = { products: [], quantities: {}, prices: {}, cartId: "", storeRefused: false, editablePrices: true };
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

function addLine(lines, product, quantity, cost) {
  if (quantity <= 0) return;
  const line = document.createElement("li");
  const what = document.createElement("span");
  what.textContent = `${quantity} × ${product.name}`;
  const amount = document.createElement("span");
  amount.textContent = cost.toFixed(2);
  line.append(what, amount);
  lines.append(line);
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
    card.dataset.selected = String(quantity > 0);
    const name = document.createElement("h3");
    name.textContent = product.name;
    // The Risky store has no payment bugs, so it shows a plain price, like the Main store.
    if (!state.editablePrices) {
      const plain = document.createElement("p");
      plain.className = "price";
      plain.textContent = `${product.price} USD`;
      const qty = document.createElement("label");
      qty.textContent = "Quantity";
      const choose = document.createElement("select");
      choose.setAttribute("aria-label", `Quantity of ${product.name}`);
      for (let n = 0; n <= 3; n += 1) choose.add(new Option(String(n), String(n)));
      choose.value = String(quantity);
      choose.addEventListener("change", () => {
        state.quantities[product.id] = Number(choose.value);
        renderCart();
      });
      qty.append(choose);
      card.append(productArt(product.id), name, plain, qty);
      grid.append(card);
      addLine(lines, product, quantity, Number(product.price) * quantity);
      total += Number(product.price) * quantity;
      continue;
    }
    // The price box is sent to the server as is; with the trust_client_price bug, the store charges it.
    const priceCell = document.createElement("label");
    priceCell.className = "price-field";
    priceCell.textContent = "Price (sent by your browser)";
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
    const qtyCell = document.createElement("label");
    qtyCell.textContent = "Quantity";
    const select = document.createElement("select");
    select.setAttribute("aria-label", `Quantity of ${product.name}`);
    for (let n = 0; n <= 3; n += 1) select.add(new Option(String(n), String(n)));
    select.value = String(quantity);
    select.addEventListener("change", () => {
      state.quantities[product.id] = Number(select.value);
      renderCart();
    });
    qtyCell.append(select);
    card.append(productArt(product.id), name, priceCell, qtyCell);
    grid.append(card);
    const cost = Number(state.prices[product.id]) * quantity;
    addLine(lines, product, quantity, cost);
    total += cost;
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

// Whether a demo bug is ticked on the page (the server holds the real switch; the box mirrors it).
const bugOn = (id) => Boolean(document.querySelector(`#bugs input[value="${id}"]`)?.checked);

async function renderBugs() {
  const bugs = await api("/api/bugs");
  const box = $("bugs");
  box.replaceChildren();
  // The Risky store has no payment bugs to switch on; hide the empty list.
  box.closest("section").hidden = bugs.available.length === 0;
  state.editablePrices = bugs.available.length > 0;
  for (const bug of bugs.available) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = bug.id;
    input.checked = bugs.active.includes(bug.id);
    input.addEventListener("change", async () => {
      const active = [...box.querySelectorAll("input:checked")].map((item) => item.value);
      await api("/api/bugs", { method: "POST", body: JSON.stringify({ bugs: active }) });
      // PayPal's buttons are loaded for one intent (capture or authorize), so switching it reloads them.
      if (bug.id === "authorize_and_forget") window.location.reload();
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
    // The authorize_and_forget bug creates AUTHORIZE orders, which PayPal's buttons must be loaded for.
    const intent = bugOn("authorize_and_forget") ? "authorize" : "capture";
    const params = new URLSearchParams({ "client-id": clientId, currency, intent });
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
        // The store, not PayPal, refused: say so, and keep PayPal's own error handler from relabelling it.
        state.storeRefused = true;
        setStatus("error", /already been paid/.test(error.message)
          ? `${error.message} Click "Start a new cart", or tick "Let the same cart be paid multiple times" to demo a duplicate charge.`
          : `The store refused the order: ${error.message}`);
        throw error;
      });
      showOrder({ id: order.id, status: "PENDING" });
      setStatus("approving", `Order ${order.id} for ${order.total} ${order.currency}. Approve it in the PayPal window.`);
      return order.id;
    },
    async onApprove(data) {
      setStatus("capturing", bugOn("authorize_and_forget") ? "Holding payment..." : "Capturing payment...");
      try {
        const order = await api(`/api/orders/${data.orderID}/capture`, { method: "POST" });
        showOrder(order);
        if (order.status !== "PAID") {
          setStatus("error", `Payment not completed (${order.status}).`);
        } else if (bugOn("allow_double_payment")) {
          // The double-payment bug keeps the paid cart, so it can be charged again.
          setStatus("paid", `Payment complete. Order ${order.order_id} is paid. The cart is kept, so every new payment charges it again.`);
        } else {
          saveCartId(crypto.randomUUID());
          setStatus("paid", `Payment complete. Order ${order.order_id} is paid. A new cart has been started.`);
        }
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
      if (state.storeRefused) {
        state.storeRefused = false;
        return;
      }
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
    // A remembered cart that was already paid gets replaced, unless the double-payment bug is on.
    const cartOrders = await api(`/api/carts/${state.cartId}/orders`).catch(() => []);
    if (!bugOn("allow_double_payment") && cartOrders.some((order) => order.status === "PAID")) {
      saveCartId(crypto.randomUUID());
    }
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
