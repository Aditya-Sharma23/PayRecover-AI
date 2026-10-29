"""PayTest Vulnerable store: a deliberately broken copy of the Main store, for demos only.

It pays through the same PayPal Sandbox as the Main store but contains real bugs,
so PayTest can be shown catching each one. Never deploy it as a real shop.

Payment bugs, switched on and off from the store page (POST /api/bugs) and saved:
  trust_client_price    charge the price the browser sends, so a buyer can edit it
  confirm_without_capture  show "paid" without ever asking PayPal to capture
  allow_double_payment  let one cart be paid again and again (no cart lock)

Always present, because this is the store's vulnerable build:
  a search box that echoes ?q= back unescaped (reflected XSS)
  no security headers (no Content-Security-Policy, no clickjacking protection)
"""

import json
import os
import re
import tempfile
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path as PathParam, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
# Reuse the Main store's Sandbox keys when this store has no .env of its own.
load_dotenv(ROOT.parent / "main-store" / ".env")

SANDBOX_API = "https://api-m.sandbox.paypal.com"
CURRENCY = "USD"
CATALOG = {
    "tee": {"name": "PayTest T-shirt", "price": Decimal("18.00")},
    "mug": {"name": "Evidence mug", "price": Decimal("12.50")},
    "stickers": {"name": "Sticker pack", "price": Decimal("4.99")},
}
MAX_QUANTITY = 10
ORDER_ID_PATTERN = r"^[A-Z0-9]{8,40}$"
CART_ID_PATTERN = r"^[a-z0-9-]{8,64}$"
BUG_NAMES = ("trust_client_price", "confirm_without_capture", "allow_double_payment")
BUG_LABELS = {
    "trust_client_price": "Charge the price the browser sends (buyer can edit prices)",
    "confirm_without_capture": "Show “paid” without capturing the money at PayPal",
    "allow_double_payment": "Let the same cart be paid more than once",
}

app = FastAPI(title="PayTest Vulnerable store", version="1.0.0")
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}


def _data_file() -> Path:
    return Path(os.environ.get("VULN_STORE_DATA_FILE") or ROOT / "data" / "store.json")


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_data_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"orders": {}, "bugs": []}
    return {"orders": data.get("orders", {}), "bugs": [b for b in data.get("bugs", []) if b in BUG_NAMES]}


def save() -> None:
    path = _data_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump({"orders": orders, "bugs": sorted(active_bugs)}, handle, indent=2)
    os.replace(handle.name, path)


_state = _load()
orders: dict[str, dict[str, Any]] = _state["orders"]
active_bugs: set[str] = set(_state["bugs"])


@app.middleware("http")
async def no_cache(request, call_next):
    # Deliberately no security headers here; only caching is controlled, so demos never show a stale page.
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


class CartLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(pattern="^[a-z]{1,20}$")
    quantity: int = Field(ge=1, le=MAX_QUANTITY)
    unit_price: str | None = Field(default=None, pattern=r"^\d{1,6}(\.\d{1,2})?$")


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CartLine] = Field(min_length=1, max_length=len(CATALOG))
    cart_id: str | None = Field(default=None, pattern=CART_ID_PATTERN)


class BugsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bugs: list[Literal[BUG_NAMES]] = Field(max_length=len(BUG_NAMES))


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _credentials() -> tuple[str, str]:
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    secret = os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
    if not client_id or not secret:
        raise HTTPException(status_code=503, detail="Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (Sandbox).")
    return client_id, secret


async def _access_token(client: httpx.AsyncClient) -> str:
    if _token_cache["token"] and time.monotonic() < _token_cache["expires_at"]:
        return _token_cache["token"]
    response = await client.post(
        f"{SANDBOX_API}/v1/oauth2/token", auth=_credentials(), data={"grant_type": "client_credentials"}
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"PayPal token request failed (HTTP {response.status_code}).")
    data = response.json()
    _token_cache.update(token=data["access_token"], expires_at=time.monotonic() + int(data["expires_in"]) - 60)
    return data["access_token"]


async def paypal_request(method: str, path: str, body: dict | None = None, request_id: str | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            headers = {"Authorization": f"Bearer {await _access_token(client)}", "Content-Type": "application/json"}
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            response = await client.request(method, f"{SANDBOX_API}{path}", json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="PayPal Sandbox could not be reached.") from exc
    if response.status_code >= 400:
        try:
            issue = response.json().get("details", [{}])[0].get("issue") or response.json().get("name")
        except (ValueError, IndexError, AttributeError):
            issue = None
        raise HTTPException(status_code=502, detail=issue or f"PayPal request failed (HTTP {response.status_code}).")
    return response.json()


def price_cart(items: list[CartLine]) -> tuple[list[dict], Decimal]:
    lines, total = [], Decimal(0)
    for item in items:
        product = CATALOG.get(item.product_id)
        if product is None:
            raise HTTPException(status_code=422, detail=f"Unknown product {item.product_id}.")
        price = product["price"]
        if "trust_client_price" in active_bugs and item.unit_price is not None:
            price = Decimal(item.unit_price)  # BUG: the browser decides what it pays.
        lines.append({"product_id": item.product_id, "name": product["name"], "quantity": item.quantity, "unit_price": price})
        total += price * item.quantity
    return lines, total


OrderId = PathParam(pattern=ORDER_ID_PATTERN)
CartId = PathParam(pattern=CART_ID_PATTERN)


def _known_order(order_id: str) -> dict[str, Any]:
    record = orders.get(order_id)
    if record is None:
        raise HTTPException(status_code=404, detail="This store has no such order.")
    return record


def _paid_order_for_cart(cart_id: str | None, exclude: str | None = None) -> dict[str, Any] | None:
    if not cart_id:
        return None
    return next((r for r in orders.values()
                 if r.get("cart_id") == cart_id and r["status"] == "PAID" and r["order_id"] != exclude), None)


@app.get("/api/config")
def config() -> dict[str, Any]:
    client_id, _ = _credentials()
    return {"paypal_client_id": client_id, "currency": CURRENCY}


@app.get("/api/products")
def products() -> list[dict[str, str]]:
    return [{"id": key, "name": item["name"], "price": _money(item["price"])} for key, item in CATALOG.items()]


@app.get("/api/bugs")
def get_bugs() -> dict[str, Any]:
    return {"active": sorted(active_bugs), "available": [{"id": b, "label": BUG_LABELS[b]} for b in BUG_NAMES]}


@app.post("/api/bugs")
def set_bugs(request: BugsRequest) -> dict[str, Any]:
    """Demo control: which payment bugs the store has. Open to anyone, because this store is only for demos."""
    active_bugs.clear()
    active_bugs.update(request.bugs)
    save()
    return get_bugs()


@app.post("/api/orders")
async def create_order(request: CreateOrderRequest) -> dict[str, Any]:
    paid = _paid_order_for_cart(request.cart_id)
    if paid is not None and "allow_double_payment" not in active_bugs:
        raise HTTPException(status_code=409, detail=f"This cart has already been paid (order {paid['order_id']}).")
    lines, total = price_cart(request.items)
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "description": "PayTest Vulnerable store order",
            "amount": {
                "currency_code": CURRENCY,
                "value": _money(total),
                "breakdown": {"item_total": {"currency_code": CURRENCY, "value": _money(total)}},
            },
            "items": [{
                "name": line["name"], "sku": line["product_id"], "quantity": str(line["quantity"]),
                "unit_amount": {"currency_code": CURRENCY, "value": _money(line["unit_price"])},
            } for line in lines],
        }],
    }
    order = await paypal_request("POST", "/v2/checkout/orders", body)
    if not re.fullmatch(ORDER_ID_PATTERN, str(order.get("id", ""))):
        raise HTTPException(status_code=502, detail="PayPal returned an unexpected order id.")
    orders[order["id"]] = {
        "order_id": order["id"], "status": "PENDING", "total": _money(total), "currency": CURRENCY,
        "items": [{**line, "unit_price": _money(line["unit_price"])} for line in lines],
        "capture_id": None, "cart_id": request.cart_id,
    }
    save()
    return {"id": order["id"], "total": _money(total), "currency": CURRENCY}


@app.post("/api/orders/{order_id}/capture")
async def capture_order(order_id: str = OrderId) -> dict[str, Any]:
    record = _known_order(order_id)
    if record["status"] == "PAID":
        return record
    if "confirm_without_capture" in active_bugs:
        record["status"] = "PAID"  # BUG: the store says paid; PayPal was never asked to capture.
        save()
        return record
    if "allow_double_payment" not in active_bugs and _paid_order_for_cart(record.get("cart_id"), exclude=order_id):
        raise HTTPException(status_code=409, detail="This cart has already been paid.")
    result = await paypal_request("POST", f"/v2/checkout/orders/{order_id}/capture", {}, f"capture-{order_id}")
    try:
        capture = result["purchase_units"][0]["payments"]["captures"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="PayPal returned no capture for this order.") from exc
    record["capture_id"] = capture["id"]
    record["status"] = "PAID" if capture["status"] == "COMPLETED" else f"CAPTURE_{capture['status']}"
    save()
    return record


@app.post("/api/orders/{order_id}/cancel")
def cancel_order(order_id: str = OrderId) -> dict[str, Any]:
    record = _known_order(order_id)
    if record["status"] == "PENDING":
        record["status"] = "CANCELLED"
        save()
    return record


@app.get("/api/orders/{order_id}")
def get_order(order_id: str = OrderId) -> dict[str, Any]:
    return _known_order(order_id)


@app.get("/api/carts/{cart_id}/orders")
def cart_orders(cart_id: str = CartId) -> list[dict[str, Any]]:
    return [record for record in orders.values() if record.get("cart_id") == cart_id]


@app.get("/", include_in_schema=False)
def index(q: str = Query(default="", max_length=500)) -> HTMLResponse:
    """The store page. BUG: the search term is pasted into the page without escaping (reflected XSS)."""
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    results = f"<p id=\"search-results\">You searched for: {q}</p>" if q else ""
    return HTMLResponse(page.replace("{{SEARCH_RESULTS}}", results))


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
