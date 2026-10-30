"""PayRecover Main store: a small, correct checkout that pays through PayPal Sandbox.

This is the application PayRecover tests, kept separate from PayRecover itself. Prices
come from the server-side catalog (a price sent by the browser is ignored), the
order is created and captured on the server, and the store records what it
*believes* happened so PayRecover can compare that belief with PayPal's own record.

The store is meant to pass every PayRecover check: every page sends security headers,
every response code is documented in the OpenAPI schema, orders are saved to
a local file so they survive a restart, and a cart can be paid only once. Deliberately vulnerable stores, for showing
PayRecover catching bugs, are built separately.
"""

import json
import os
import re
import secrets
import tempfile
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path as PathParam, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

SANDBOX_API = "https://api-m.sandbox.paypal.com"
CURRENCY = "USD"
CATALOG = {
    "tee": {"name": "PayRecover T-shirt", "price": Decimal("18.00")},
    "mug": {"name": "Evidence mug", "price": Decimal("12.50")},
    "stickers": {"name": "Sticker pack", "price": Decimal("4.99")},
}
MAX_QUANTITY = 10
ORDER_ID_PATTERN = r"^[A-Z0-9]{8,40}$"
CART_ID_PATTERN = r"^[a-z0-9-]{8,64}$"

# PayPal's checkout script, buttons and popup need these origins; everything else is same-origin.
PAYPAL_SOURCES = "https://*.paypal.com https://*.paypalobjects.com https://*.venmo.com"


def content_security_policy(nonce: str | None = None) -> str:
    """The store's CSP. The page gets a fresh nonce, which PayPal's SDK puts on the inline scripts it adds."""
    script_nonce = f" 'nonce-{nonce}'" if nonce else ""
    return "; ".join((
        "default-src 'self'",
        f"script-src 'self'{script_nonce} {PAYPAL_SOURCES}",
        f"connect-src 'self' {PAYPAL_SOURCES}",
        f"frame-src {PAYPAL_SOURCES}",
        f"img-src 'self' data: {PAYPAL_SOURCES}",
        # The PayPal SDK styles its buttons with inline styles.
        "style-src 'self' 'unsafe-inline'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ))


SECURITY_HEADERS = {
    "Content-Security-Policy": content_security_policy(),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": 'camera=(), microphone=(), geolocation=(), payment=(self "https://www.paypal.com")',
    # Lets the PayPal popup report back to this page, without opening the page to other windows.
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
}


class ErrorBody(BaseModel):
    detail: str


def _errors(*codes: int) -> dict[int, dict[str, Any]]:
    """Document every error code an endpoint can return, so the OpenAPI schema is complete."""
    text = {
        404: "This store has no such order.",
        409: "This cart has already been paid.",
        422: "The request did not match the schema.",
        502: "PayPal could not be reached or rejected the request.",
        503: "PayPal Sandbox credentials are not configured.",
    }
    return {code: {"model": ErrorBody, "description": text[code]} for code in codes}


app = FastAPI(title="PayRecover Main store", version="2.0.0")
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    # Browsers revalidate the page and its scripts, so an update never runs against a stale copy.
    response.headers.setdefault("Cache-Control", "no-cache")
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def _orders_file() -> Path:
    return Path(os.environ.get("STORE_ORDERS_FILE") or ROOT / "data" / "orders.json")


def _load_orders() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_orders_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_orders() -> None:
    """Write every order atomically, so a crash mid-write never leaves a half-written file."""
    path = _orders_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(orders, handle, indent=2)
    os.replace(handle.name, path)


orders: dict[str, dict[str, Any]] = _load_orders()


class CartLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(pattern="^[a-z]{1,20}$")
    quantity: int = Field(ge=1, le=MAX_QUANTITY)
    # Accepted for compatibility with clients that send a display price, and always ignored:
    # the server charges its own catalog price.
    unit_price: str | None = Field(default=None, pattern=r"^\d{1,6}(\.\d{1,2})?$")


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CartLine] = Field(min_length=1, max_length=len(CATALOG))
    # The browser's cart. A cart is paid at most once, so a double click or a retry never charges twice.
    cart_id: str | None = Field(default=None, pattern=CART_ID_PATTERN)


class Product(BaseModel):
    id: str
    name: str
    price: str


class StoreConfig(BaseModel):
    paypal_client_id: str
    currency: str


class CreatedOrder(BaseModel):
    id: str
    total: str
    currency: str


class OrderLine(BaseModel):
    product_id: str
    name: str
    quantity: int
    unit_price: str


class OrderRecord(BaseModel):
    order_id: str
    status: str
    total: str
    currency: str
    items: list[OrderLine]
    capture_id: str | None = None
    cart_id: str | None = None


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
                # Makes a retried capture idempotent at PayPal.
                headers["PayPal-Request-Id"] = request_id
            response = await client.request(method, f"{SANDBOX_API}{path}", json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="PayPal Sandbox could not be reached.") from exc
    if response.status_code >= 400:
        try:
            issue = response.json().get("details", [{}])[0].get("issue") or response.json().get("name")
        except (ValueError, IndexError, AttributeError):
            issue = None
        # Every PayPal failure is reported as 502, so the store's own status codes stay documented.
        raise HTTPException(status_code=502, detail=issue or f"PayPal request failed (HTTP {response.status_code}).")
    return response.json()


def price_cart(items: list[CartLine]) -> tuple[list[dict], Decimal]:
    lines, total = [], Decimal(0)
    for item in items:
        product = CATALOG.get(item.product_id)
        if product is None:
            raise HTTPException(status_code=422, detail=f"Unknown product {item.product_id}.")
        lines.append({
            "product_id": item.product_id, "name": product["name"], "quantity": item.quantity,
            "unit_price": product["price"],
        })
        total += product["price"] * item.quantity
    return lines, total


OrderId = PathParam(pattern=ORDER_ID_PATTERN, description="The PayPal order id the store returned.")
CartId = PathParam(pattern=CART_ID_PATTERN, description="The cart id the browser sent with its orders.")


def _paid_order_for_cart(cart_id: str | None, exclude: str | None = None) -> dict[str, Any] | None:
    if not cart_id:
        return None
    return next(
        (record for record in orders.values()
         if record.get("cart_id") == cart_id and record["status"] == "PAID" and record["order_id"] != exclude),
        None,
    )


def _refuse_paid_cart(cart_id: str | None, exclude: str | None = None) -> None:
    paid = _paid_order_for_cart(cart_id, exclude)
    if paid is not None:
        raise HTTPException(status_code=409, detail=f"This cart has already been paid (order {paid['order_id']}).")


@app.get("/api/config", response_model=StoreConfig, responses=_errors(503))
def config() -> dict[str, Any]:
    """Public values the page needs. The client id is public by design; the secret never leaves the server."""
    client_id, _ = _credentials()
    return {"paypal_client_id": client_id, "currency": CURRENCY}


@app.get("/api/products", response_model=list[Product])
def products() -> list[dict[str, str]]:
    return [{"id": key, "name": item["name"], "price": _money(item["price"])} for key, item in CATALOG.items()]


@app.post("/api/orders", response_model=CreatedOrder, responses=_errors(409, 502, 503))
async def create_order(request: CreateOrderRequest) -> dict[str, Any]:
    _refuse_paid_cart(request.cart_id)
    lines, total = price_cart(request.items)
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "description": "PayRecover Main store order",
            "amount": {
                "currency_code": CURRENCY,
                "value": _money(total),
                "breakdown": {"item_total": {"currency_code": CURRENCY, "value": _money(total)}},
            },
            "items": [{
                "name": line["name"],
                "sku": line["product_id"],
                "quantity": str(line["quantity"]),
                "unit_amount": {"currency_code": CURRENCY, "value": _money(line["unit_price"])},
            } for line in lines],
        }],
    }
    order = await paypal_request("POST", "/v2/checkout/orders", body)
    if not re.fullmatch(ORDER_ID_PATTERN, str(order.get("id", ""))):
        raise HTTPException(status_code=502, detail="PayPal returned an unexpected order id.")
    orders[order["id"]] = {
        "order_id": order["id"],
        "status": "PENDING",
        "total": _money(total),
        "currency": CURRENCY,
        "items": [{**line, "unit_price": _money(line["unit_price"])} for line in lines],
        "capture_id": None,
        "cart_id": request.cart_id,
    }
    save_orders()
    return {"id": order["id"], "total": _money(total), "currency": CURRENCY}


def _known_order(order_id: str) -> dict[str, Any]:
    record = orders.get(order_id)
    if record is None:
        raise HTTPException(status_code=404, detail="This store has no such order.")
    return record


@app.post("/api/orders/{order_id}/capture", response_model=OrderRecord, responses=_errors(404, 409, 502, 503))
async def capture_order(order_id: str = OrderId) -> dict[str, Any]:
    record = _known_order(order_id)
    if record["status"] == "PAID":
        return record
    # Two orders opened for one cart (two tabs, a double click): only the first is ever captured.
    _refuse_paid_cart(record.get("cart_id"), exclude=order_id)
    result = await paypal_request("POST", f"/v2/checkout/orders/{order_id}/capture", {}, f"capture-{order_id}")
    try:
        capture = result["purchase_units"][0]["payments"]["captures"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="PayPal returned no capture for this order.") from exc
    record["capture_id"] = capture["id"]
    # Only a COMPLETED capture is a payment; anything else is recorded as it is.
    record["status"] = "PAID" if capture["status"] == "COMPLETED" else f"CAPTURE_{capture['status']}"
    save_orders()
    return record


@app.post("/api/orders/{order_id}/cancel", response_model=OrderRecord, responses=_errors(404))
def cancel_order(order_id: str = OrderId) -> dict[str, Any]:
    record = _known_order(order_id)
    if record["status"] == "PENDING":
        record["status"] = "CANCELLED"
        save_orders()
    return record


@app.get("/api/orders/{order_id}", response_model=OrderRecord, responses=_errors(404))
def get_order(order_id: str = OrderId) -> dict[str, Any]:
    """The store's own claim about an order. PayRecover checks it against PayPal."""
    return _known_order(order_id)


@app.get("/api/carts/{cart_id}/orders", response_model=list[OrderRecord])
def cart_orders(cart_id: str = CartId) -> list[dict[str, Any]]:
    """Every order opened for one cart, so a checker can see whether the cart was charged twice."""
    return [record for record in orders.values() if record.get("cart_id") == cart_id]


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    """The checkout page, with a fresh CSP nonce that store.js hands to PayPal's SDK."""
    nonce = secrets.token_urlsafe(18)
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8").replace("{{CSP_NONCE}}", nonce)
    return HTMLResponse(page, headers={"Content-Security-Policy": content_security_policy(nonce)})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """No icon; answering 204 keeps a 404 out of every visitor's console."""
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
