"""Connect your own store: Gemini reads the store's order data and works out how to read it.

PayRecover's demo stores speak PayRecover's format. A real shop does not: its orders API names
fields its own way ("state", "amount.total", "paypal.orderId"), nests lists, and may count money
in cents. Instead of asking the merchant to write an adapter, PayRecover fetches a sample of the
store's orders (and products), and Gemini proposes a mapping: which field is the PayPal order id,
the status and which statuses mean paid, the total, the line items, and so on.

The AI only proposes. PayRecover then checks the mapping on every fetched order, and asks PayPal
about a few orders the mapping says are paid: does the order exist, and does its amount match?
Only a mapping that passes is saved. After that the store is the "connected" store, and every
scan, investigation and recovery reads it through the mapping, exactly as it reads a demo store.

Fetching follows the same address rules as website tests: on a public deployment only public
addresses, pinned to the addresses that were checked; in local mode a store on this machine is
allowed. Requests are GET only, so connecting never changes anything in the store. Values that
look like emails or phone numbers are masked before the sample reaches the AI.
"""

import json
import os
import re
import time
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from app import ai, alerts, api_runner, paypal
from app.deployment import public_mode
from app.schemas import (
    CheckStatus,
    ConnectorProposal,
    ConnectorRequest,
    PaymentCheck,
    StoreMapping,
)
from app.security import LOCAL_OWNER
from app.url_policy import UnsafeTarget, trusted_origin

SAVE_FILE = "connected-stores.json"
FETCH_TIMEOUT_SECONDS = 15
SAMPLE_ITEMS = 8
SAMPLE_TEXT = 160
PREVIEW_ORDERS = 5
PAYPAL_CHECKS = 3
CACHE_SECONDS = 5
PAYPAL_ORDER_ID = re.compile(r"^[A-Z0-9]{8,40}$")
EMAIL = re.compile(r"[^@\s\"']+@[^@\s\"']+\.[a-z]{2,}", re.IGNORECASE)
# A phone number stands alone and has a leading + or separators, so PayPal ids such as 9ZZ00000000000002 are kept.
PHONE = re.compile(r"(?<![\w])(?:\+\d[\d\s().-]{7,}\d|\d{2,4}[\s().-]\d[\d\s().-]{5,}\d)(?![\w])")
# Ids and timestamps say nothing about meaning, so the value summary leaves them out.
ID_LIKE = re.compile(r"\d{4}|^[A-Z0-9]{8,}$")
# Whose connected store a request means. Set per request by the security middleware; auto-scan is local.
current_owner: ContextVar[str] = ContextVar("connector_owner", default=LOCAL_OWNER)
_cache: dict[str, tuple[float, int, Any]] = {}


OPTIONAL_ORDER_FIELDS = ("currency", "items", "item_product_id", "item_quantity", "item_unit_price", "cart_id",
                         "created_at", "capture_id")


class ConnectorError(RuntimeError):
    """The store could not be read, or its data did not make sense."""


def example() -> ConnectorRequest:
    """The demo to try: the Vulnerable store's orders in the ShopLegacy v2 export format (other names, cents)."""
    base = os.environ.get("TESTPILOT_VULN_STORE_URL", "http://127.0.0.1:8200").strip().rstrip("/")
    return ConnectorRequest(name="ShopLegacy demo shop", base_url=base, orders_path="/shoplegacy/v2/sales?count=100",
                            products_path="/shoplegacy/v2/catalog")


# ---------- saved connections ----------

def saved(owner: str | None = None) -> StoreMapping | None:
    raw = alerts._load(SAVE_FILE).get(owner or current_owner.get())
    return StoreMapping.model_validate(raw) if raw else None


def connected(owner: str | None = None) -> bool:
    return saved(owner) is not None


def save(mapping: StoreMapping, owner: str) -> None:
    data = alerts._load(SAVE_FILE)
    data[owner] = mapping.model_dump(mode="json")
    alerts._save(SAVE_FILE, data)


def forget(owner: str) -> bool:
    data = alerts._load(SAVE_FILE)
    removed = data.pop(owner, None) is not None
    alerts._save(SAVE_FILE, data)
    return removed


# ---------- reading the store ----------

def _clean_base(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConnectorError("The store address must start with http:// or https://.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConnectorError("Give only the store's address, without a login, query or #fragment.")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


async def fetch_json(url: str) -> tuple[int, Any]:
    """One GET to the store, through the pinned, address-checked client. Cached a few seconds per URL."""
    hit = _cache.get(url)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1], hit[2]

    async def get():
        return await api_runner._fetch_api(url, FETCH_TIMEOUT_SECONDS)

    try:
        if public_mode():
            response = await get()
        else:
            # Local mode: the person running PayRecover may connect a store on this machine.
            with trusted_origin(url):
                response = await get()
    except UnsafeTarget as exc:
        raise ConnectorError(f"PayRecover will not read that address: {exc}.") from exc
    except ValueError as exc:  # the 2 MB response limit
        raise ConnectorError(f"The store's answer was too large: {exc}.") from exc
    except Exception as exc:  # network failures
        raise ConnectorError(f"The store at {url} could not be reached.") from exc
    try:
        data = json.loads(response.body.decode("utf-8", "replace")) if response.body else None
    except json.JSONDecodeError as exc:
        if response.status_code >= 400:
            return response.status_code, None
        raise ConnectorError(f"{url} did not answer with JSON.") from exc
    _cache[url] = (time.monotonic(), response.status_code, data)
    return response.status_code, data


def pick(value: Any, path: str | None) -> Any:
    """A value at a dotted path such as "amount.total" or "lines.0.sku"; None when it is missing."""
    if not path:
        return value
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _money(value: Any, money: str) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if money == "cents":
        amount = amount / 100
    return str(amount.quantize(Decimal("0.01")))


def normalize_order(raw: Any, mapping: StoreMapping) -> dict[str, Any] | None:
    """One store order in PayRecover's own format, or None when its id, status or total is missing."""
    if not isinstance(raw, dict):
        return None
    fields = mapping.order_fields
    order_id = pick(raw, fields.order_id)
    status = pick(raw, fields.status)
    total = _money(pick(raw, fields.total), mapping.money)
    if order_id is None or status is None or total is None:
        return None
    paid = str(status).strip().lower() in {value.strip().lower() for value in mapping.paid_statuses}
    lines = pick(raw, fields.items) if fields.items else None
    items = []
    for line in lines if isinstance(lines, list) and fields.item_product_id else []:
        if not isinstance(line, dict):
            continue
        quantity = pick(line, fields.item_quantity)
        try:
            quantity = int(quantity) if quantity is not None else 1
        except (TypeError, ValueError):
            quantity = 1
        product_id = str(pick(line, fields.item_product_id) or "")
        items.append({"product_id": product_id, "name": product_id, "quantity": quantity,
                      "unit_price": _money(pick(line, fields.item_unit_price), mapping.money)})
    return {
        "order_id": str(order_id).strip(),
        "status": "PAID" if paid else str(status).strip().upper(),
        "total": total,
        "currency": str(pick(raw, fields.currency) or mapping.currency).upper() if fields.currency else mapping.currency,
        "items": [item for item in items if item["product_id"] and item["unit_price"] is not None],
        "cart_id": str(pick(raw, fields.cart_id)) if fields.cart_id and pick(raw, fields.cart_id) else None,
        "created_at": str(pick(raw, fields.created_at)) if fields.created_at and pick(raw, fields.created_at) else None,
        "capture_id": str(pick(raw, fields.capture_id)) if fields.capture_id and pick(raw, fields.capture_id) else None,
    }


def normalize_product(raw: Any, mapping: StoreMapping) -> dict[str, Any] | None:
    fields = mapping.product_fields
    if fields is None or not isinstance(raw, dict):
        return None
    product_id, price = pick(raw, fields.id), _money(pick(raw, fields.price), mapping.money)
    if product_id is None or price is None:
        return None
    name = str(pick(raw, fields.name) or product_id) if fields.name else str(product_id)
    return {"id": str(product_id), "name": name, "price": price, "market": name}


async def _orders(mapping: StoreMapping) -> list[dict[str, Any]]:
    status, data = await fetch_json(mapping.base_url + mapping.orders_path)
    if status >= 400:
        raise ConnectorError(f"The store answered HTTP {status} for {mapping.orders_path}.")
    listed = pick(data, mapping.orders_list)
    if not isinstance(listed, list):
        raise ConnectorError(f"No list of orders at {mapping.orders_list or 'the top of the answer'}.")
    return [order for order in (normalize_order(raw, mapping) for raw in listed) if order]


async def store_json(method: str, path: str) -> tuple[int, Any]:
    """Answer one PayRecover store request (GET /api/...) for the connected store, through its mapping."""
    mapping = saved()
    if mapping is None:
        raise ConnectorError("No store is connected yet. Connect one under Check payments > Connect your store.")
    if method != "GET":
        # PayRecover only reads a connected store; probes that create orders run on the demo stores only.
        return 405, {"detail": "PayRecover only reads a connected store."}
    route, _, query = path.partition("?")
    if route == "/api/recent-orders":
        params = parse_qs(query)
        limit = int((params.get("limit") or ["25"])[0])
        wanted = (params.get("status") or [None])[0]
        return 200, [order for order in await _orders(mapping) if wanted is None or order["status"] == wanted][:limit]
    if route == "/api/products":
        if not mapping.products_path:
            return 404, {"detail": "This store's product list is not connected."}
        status, data = await fetch_json(mapping.base_url + mapping.products_path)
        listed = pick(data, mapping.products_list)
        if status >= 400 or not isinstance(listed, list):
            return 404, {"detail": "The store's product list could not be read."}
        return 200, [product for product in (normalize_product(raw, mapping) for raw in listed) if product]
    match = re.fullmatch(r"/api/carts/([^/]+)/orders", route)
    if match:
        if not mapping.order_fields.cart_id:
            return 404, {"detail": "This store does not record a cart per order."}
        return 200, [order for order in await _orders(mapping) if order["cart_id"] == match.group(1)]
    match = re.fullmatch(r"/api/orders/([^/]+)", route)
    if match:
        order_id = match.group(1)
        if mapping.order_path:
            status, data = await fetch_json(mapping.base_url + mapping.order_path.replace("{id}", quote(order_id, safe="")))
            if status == 404:
                return 404, {"detail": "The store has no such order."}
            order = normalize_order(pick(data, mapping.order_object), mapping) if status < 400 else None
            if order is not None:
                return 200, order
        found = next((order for order in await _orders(mapping) if order["order_id"] == order_id), None)
        return (200, found) if found else (404, {"detail": "The store has no such order."})
    return 404, {"detail": f"PayRecover does not read {route} from a connected store."}


# ---------- the AI proposal, and the checks that decide it ----------

def _redact(value: Any) -> Any:
    """Shorten the sample, and mask what looks like an email or a phone number, before the AI sees it."""
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in list(value.items())[:40]}
    if isinstance(value, list):
        # Spread across the list, not just the newest: the newest orders are often all in one state.
        step = max(1, len(value) // SAMPLE_ITEMS)
        return [_redact(item) for item in value[::step][:SAMPLE_ITEMS]]
    if isinstance(value, str):
        text = EMAIL.sub("<email>", value)
        text = PHONE.sub("<phone>", text)
        return text[:SAMPLE_TEXT]
    return value


def _largest_list(value: Any, depth: int = 0) -> list | None:
    """The biggest list of objects in an answer: almost always the orders."""
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        return value
    if isinstance(value, dict) and depth < 4:
        found = [_largest_list(item, depth + 1) for item in value.values()]
        found = [item for item in found if item]
        return max(found, key=len) if found else None
    return None


def _leaf_values(value: Any, prefix: str, out: dict[str, set], depth: int = 0) -> None:
    if isinstance(value, dict) and depth < 4:
        for key, item in value.items():
            _leaf_values(item, f"{prefix}.{key}" if prefix else str(key), out, depth + 1)
    elif isinstance(value, str) and len(value) <= 40 and not EMAIL.search(value) and not PHONE.search(value):
        out.setdefault(prefix, set()).add(value)


def value_summary(data: Any) -> dict[str, list[str]]:
    """Every distinct short text value per field across all orders, so the AI sees every status that occurs,
    not only the ones in its small sample. Ids, dates and fields with many values are left out."""
    orders = _largest_list(data) or []
    seen: dict[str, set] = {}
    for order in orders:
        _leaf_values(order, "", seen)
    return {path: sorted(values) for path, values in seen.items()
            if 1 <= len(values) <= 12 and not any(ID_LIKE.search(value) for value in values)}


def _prompt(base: str, orders_path: str, orders_sample: Any, products_path: str | None, products_sample: Any,
            values: dict[str, list[str]] | None = None) -> str:
    schema = {
        "orders_list": "dotted path to the list of orders in the orders answer, or null when the answer is the list",
        "order_object": "dotted path to the order inside a single-order answer, or null",
        "order_path": "path of one order with {id} for the PayPal order id, if the store has one (e.g. /orders/{id}), or null",
        "products_list": "dotted path to the list of products in the products answer, or null",
        "money": "\"decimal\" when amounts look like 18.00, \"cents\" when they look like 1800",
        "currency": "three-letter currency to assume when orders do not say, e.g. USD",
        "paid_statuses": ["every status value that means the store believes the order is paid"],
        "paypal_custom_id": "the custom_id the store sends PayPal, if it appears in the data, else null",
        "order_fields": {"order_id": "path of the PayPal order id (8 to 40 capital letters and digits)",
                         "status": "path", "total": "path", "currency": "path or null", "items": "path of the line list or null",
                         "item_product_id": "path inside a line, or null", "item_quantity": "path inside a line, or null",
                         "item_unit_price": "path inside a line, or null", "cart_id": "path or null",
                         "created_at": "path or null", "capture_id": "path of the PayPal capture id, or null"},
        "product_fields": {"id": "path", "price": "path", "name": "path or null"},
    }
    return f"""Return only JSON matching this schema:
{json.dumps(schema)}
You are connecting a merchant's online store to PayRecover, which checks every order the store calls paid against PayPal.
Work out from the sample how to read this store's data. Paths are dotted, relative to one order (or one product or line),
like "amount.total" or "lines". Use only fields that appear in the sample; use null when something is not there.
The PayPal order id is the value that looks like 5O190127TN364715T, not the store's own order number.
paid_statuses must contain only values that appear for the status field (see the distinct values), and only those
meaning the money was received.
Store: {base}
Distinct values each order field takes across ALL orders, not just the sample (use these for paid_statuses): {json.dumps(values or {})[:2000]}
Orders answer ({orders_path}): {json.dumps(orders_sample, default=str)[:6000]}
Products answer ({products_path or "not given"}): {json.dumps(products_sample, default=str)[:3000] if products_path else "null"}"""


def _mapping_from(value: dict[str, Any], request: ConnectorRequest, base: str) -> StoreMapping:
    fields = value.get("order_fields") or {}
    products = value.get("product_fields") if request.products_path else None
    return StoreMapping.model_validate({
        "name": request.name, "base_url": base, "orders_path": request.orders_path,
        "order_path": value.get("order_path") or None, "products_path": request.products_path or None,
        "orders_list": value.get("orders_list") or None, "order_object": value.get("order_object") or None,
        "products_list": value.get("products_list") or None,
        "money": value.get("money") if value.get("money") in {"decimal", "cents"} else "decimal",
        "currency": str(value.get("currency") or "USD")[:3].upper(),
        "paid_statuses": [str(item) for item in (value.get("paid_statuses") or [])][:10],
        "paypal_custom_id": value.get("paypal_custom_id") or None,
        "order_fields": {key: fields.get(key) or None for key in OPTIONAL_ORDER_FIELDS}
                        | {key: str(fields.get(key) or "") for key in ("order_id", "status", "total")},
        "product_fields": products if isinstance(products, dict) and products.get("id") and products.get("price") else None,
    })


def _check(name: str, ok: bool | None, message: str, evidence: dict | None = None) -> PaymentCheck:
    status = CheckStatus.SKIPPED if ok is None else CheckStatus.PASSED if ok else CheckStatus.FAILED
    return PaymentCheck(name=name, status=status, message=message, evidence=evidence or {})


async def verify(mapping: StoreMapping) -> tuple[list[PaymentCheck], list[dict[str, Any]]]:
    """Read the store through the mapping and check it, with PayPal as the referee. Deterministic."""
    status, data = await fetch_json(mapping.base_url + mapping.orders_path)
    listed = pick(data, mapping.orders_list) if status < 400 else None
    if not isinstance(listed, list):
        return [_check("orders_found", False, f"No list of orders at {mapping.orders_list or 'the top of the answer'} "
                       f"(HTTP {status}).")], []
    orders = [normalize_order(raw, mapping) for raw in listed]
    readable = [order for order in orders if order]
    paid = [order for order in readable if order["status"] == "PAID"]
    checks = [
        _check("orders_found", bool(listed), f"The store listed {len(listed)} order(s)."),
        _check("orders_readable", bool(listed) and len(readable) == len(listed),
               f"{len(readable)} of {len(listed)} order(s) have an id, a status and a total through this mapping."),
        _check("paid_orders_found", True if paid else None,
               f"{len(paid)} order(s) count as paid ({', '.join(mapping.paid_statuses)})." if paid
               else "No order has a paid status yet, so there is nothing to check against PayPal."),
    ]
    bad_ids = [order["order_id"] for order in paid if not PAYPAL_ORDER_ID.fullmatch(order["order_id"])]
    checks.append(_check("paypal_ids", not bad_ids if paid else None,
                         "Every paid order has a PayPal order id." if not bad_ids else
                         f"These do not look like PayPal order ids: {', '.join(bad_ids[:3])}."))
    if mapping.products_path:
        product_status, product_data = await fetch_json(mapping.base_url + mapping.products_path)
        listed_products = pick(product_data, mapping.products_list) if product_status < 400 else None
        products = [normalize_product(raw, mapping) for raw in listed_products] if isinstance(listed_products, list) else []
        checks.append(_check("products_readable", bool(products) and all(products),
                             f"{sum(1 for product in products if product)} product(s) have an id and a price."
                             if products else "The product list could not be read through this mapping."))
    try:
        for order in paid[:PAYPAL_CHECKS]:
            if not PAYPAL_ORDER_ID.fullmatch(order["order_id"]):
                continue
            try:
                summary = paypal.order_summary(await paypal.fetch_order(order["order_id"]))
            except paypal.PayPalOrderNotFound:
                checks.append(_check("paypal_cross_check", False, f"PayPal has no order {order['order_id']}: the order "
                                     "id field may be wrong, or the store uses another PayPal app."))
                continue
            same = summary["amount"] is not None and Decimal(str(summary["amount"])) == Decimal(order["total"])
            checks.append(_check("paypal_cross_check", same,
                                 f"PayPal has order {order['order_id']} for {summary['amount']} {summary['currency']}; "
                                 f"the mapping reads {order['total']} {order['currency']}.",
                                 {"order_id": order["order_id"], "paypal_amount": summary["amount"], "store_total": order["total"]}))
    except (paypal.PayPalConfigurationError, paypal.PayPalProviderError) as exc:
        checks.append(_check("paypal_cross_check", None, f"PayPal could not be asked ({exc}), so amounts were not cross-checked."))
    return checks, readable[:PREVIEW_ORDERS]


def ready(checks: list[PaymentCheck]) -> bool:
    return not any(check.status == CheckStatus.FAILED for check in checks) and any(
        check.name == "orders_readable" and check.status == CheckStatus.PASSED for check in checks)


async def propose(request: ConnectorRequest) -> ConnectorProposal:
    """Fetch a sample, let the AI propose a mapping, then check it. A mapping sent with the request is only checked."""
    base = _clean_base(request.base_url)
    if request.mapping is not None:
        mapping = request.mapping.model_copy(update={"base_url": base})
        checks, preview = await verify(mapping)
        return ConnectorProposal(mapping=mapping, checks=checks, preview=preview, ready=ready(checks), source="Your edit")
    status, orders_data = await fetch_json(base + request.orders_path)
    if status >= 400 or orders_data is None:
        raise ConnectorError(f"The store answered HTTP {status} for {request.orders_path}.")
    products_data = None
    if request.products_path:
        product_status, products_data = await fetch_json(base + request.products_path)
        if product_status >= 400:
            products_data = None
    prompt = _prompt(base, request.orders_path, _redact(orders_data), request.products_path, _redact(products_data),
                     value_summary(orders_data))
    try:
        mapping = _mapping_from(await ai.ask_json(prompt), request, base)
    except (ai.AIConfigurationError, ai.AIProviderError) as exc:
        raise ConnectorError(f"The AI could not propose a mapping ({exc}). You can still write one by hand.") from exc
    except ValueError as exc:
        raise ConnectorError(f"The AI's mapping was not usable ({str(exc)[:200]}). Try again, or write one by hand.") from exc
    checks, preview = await verify(mapping)
    return ConnectorProposal(mapping=mapping, checks=checks, preview=preview, ready=ready(checks),
                             source=ai.answer_source())
