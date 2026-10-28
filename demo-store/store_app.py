"""PayTest demo store: a small, controlled checkout that pays through PayPal Sandbox.

This is the application PayTest tests, kept separate from PayTest itself. Prices
come from the server-side catalog, the order is created and captured on the
server, and the store records what it *believes* happened so PayTest can compare
that belief with PayPal's own record.

Controlled defects (off by default) let a demo show PayTest catching real bugs:
  trust_client_price       use the unit price the browser sends (amount tampering)
  confirm_without_capture  mark an order paid without capturing it at PayPal
Turn them on with STORE_DEFECTS=trust_client_price,confirm_without_capture, or at
runtime through /api/defects when STORE_ADMIN_TOKEN is set.
"""

import hmac
import os
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

SANDBOX_API = "https://api-m.sandbox.paypal.com"
CURRENCY = "USD"
KNOWN_DEFECTS = ("trust_client_price", "confirm_without_capture")
CATALOG = {
    "tee": {"name": "PayTest T-shirt", "price": Decimal("18.00")},
    "mug": {"name": "Evidence mug", "price": Decimal("12.50")},
    "stickers": {"name": "Sticker pack", "price": Decimal("4.99")},
}
MAX_QUANTITY = 10

app = FastAPI(title="PayTest demo store", version="1.0.0")
orders: dict[str, dict[str, Any]] = {}
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}
_active_defects: set[str] = {
    name.strip() for name in os.environ.get("STORE_DEFECTS", "").split(",") if name.strip() in KNOWN_DEFECTS
}


class CartLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(pattern="^[a-z]{1,20}$")
    quantity: int = Field(ge=1, le=MAX_QUANTITY)
    # Sent by the page for display. Ignored unless the trust_client_price defect is on.
    unit_price: str | None = Field(default=None, pattern=r"^\d{1,6}(\.\d{1,2})?$")


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CartLine] = Field(min_length=1, max_length=len(CATALOG))


class DefectsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defects: list[str] = Field(max_length=len(KNOWN_DEFECTS))


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
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        headers = {"Authorization": f"Bearer {await _access_token(client)}", "Content-Type": "application/json"}
        if request_id:
            # Makes a retried capture idempotent at PayPal.
            headers["PayPal-Request-Id"] = request_id
        response = await client.request(method, f"{SANDBOX_API}{path}", json=body, headers=headers)
    if response.status_code >= 400:
        try:
            issue = response.json().get("details", [{}])[0].get("issue") or response.json().get("name")
        except (ValueError, IndexError, AttributeError):
            issue = None
        raise HTTPException(status_code=response.status_code, detail=issue or "PayPal request failed.")
    return response.json()


def price_cart(items: list[CartLine]) -> tuple[list[dict], Decimal]:
    lines, total = [], Decimal("0")
    for item in items:
        product = CATALOG.get(item.product_id)
        if product is None:
            raise HTTPException(status_code=422, detail=f"Unknown product {item.product_id}.")
        price = product["price"]
        if "trust_client_price" in _active_defects and item.unit_price is not None:
            price = Decimal(item.unit_price)  # DEFECT: the browser decides what it pays.
        lines.append({
            "product_id": item.product_id, "name": product["name"], "quantity": item.quantity, "unit_price": price,
        })
        total += price * item.quantity
    return lines, total


@app.get("/api/config")
def config() -> dict[str, Any]:
    """Public values the page needs. The client id is public by design; the secret never leaves the server."""
    client_id, _ = _credentials()
    return {"paypal_client_id": client_id, "currency": CURRENCY}


@app.get("/api/products")
def products() -> list[dict[str, str]]:
    return [{"id": key, "name": item["name"], "price": _money(item["price"])} for key, item in CATALOG.items()]


@app.post("/api/orders")
async def create_order(request: CreateOrderRequest) -> dict[str, Any]:
    lines, total = price_cart(request.items)
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "description": "PayTest demo store order",
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
    orders[order["id"]] = {
        "order_id": order["id"],
        "status": "PENDING",
        "total": _money(total),
        "currency": CURRENCY,
        "items": [{**line, "unit_price": _money(line["unit_price"])} for line in lines],
        "capture_id": None,
    }
    return {"id": order["id"], "total": _money(total), "currency": CURRENCY}


def _known_order(order_id: str) -> dict[str, Any]:
    record = orders.get(order_id)
    if record is None:
        raise HTTPException(status_code=404, detail="This store has no such order.")
    return record


@app.post("/api/orders/{order_id}/capture")
async def capture_order(order_id: str) -> dict[str, Any]:
    record = _known_order(order_id)
    if record["status"] == "PAID":
        return record
    if "confirm_without_capture" in _active_defects:
        record["status"] = "PAID"  # DEFECT: the store says paid; PayPal was never asked to capture.
        return record
    result = await paypal_request("POST", f"/v2/checkout/orders/{order_id}/capture", {}, f"capture-{order_id}")
    capture = result["purchase_units"][0]["payments"]["captures"][0]
    record["capture_id"] = capture["id"]
    record["status"] = "PAID" if capture["status"] == "COMPLETED" else f"CAPTURE_{capture['status']}"
    return record


@app.post("/api/orders/{order_id}/cancel")
def cancel_order(order_id: str) -> dict[str, Any]:
    record = _known_order(order_id)
    if record["status"] == "PENDING":
        record["status"] = "CANCELLED"
    return record


@app.get("/api/orders/{order_id}")
def get_order(order_id: str) -> dict[str, Any]:
    """The store's own claim about an order. PayTest checks it against PayPal."""
    return _known_order(order_id)


def _require_admin(token: str | None) -> None:
    expected = os.environ.get("STORE_ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=404, detail="Runtime defect switching is disabled.")
    if not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="A valid X-Store-Admin-Token header is required.")


@app.get("/api/defects")
def get_defects() -> dict[str, list[str]]:
    return {"active": sorted(_active_defects), "available": list(KNOWN_DEFECTS)}


@app.post("/api/defects")
def set_defects(request: DefectsRequest, x_store_admin_token: str | None = Header(default=None)) -> dict[str, list[str]]:
    _require_admin(x_store_admin_token)
    unknown = set(request.defects) - set(KNOWN_DEFECTS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown defects: {', '.join(sorted(unknown))}.")
    _active_defects.clear()
    _active_defects.update(request.defects)
    return get_defects()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
