"""Market price check with Channel3: catch prices that look like typos before they cost money on every sale.

A store that lists a mug at 1.25 instead of 12.50 is paid exactly what it asked, so PayPal's
records match and no payment check can see the problem. For each product the store sells,
PayRecover searches Channel3 (100M+ products from 25,000+ retailers) for comparable new,
in-stock products, and compares the store's price with the edges of the market, so a cheap but normal
price (most products in the range cost more, a few cost less) is not mistaken for a typo:
  * under half the price of the cheapest tenth: "underpriced", likely a typo that loses money;
  * over one and a half times the dearest tenth: "overpriced", which costs sales.
Searches are cached for six hours, so repeated checks do not use Channel3 credits.
The key is the operator's TESTPILOT_CHANNEL3_API_KEY and is never sent to the browser.
"""

import asyncio
import json
import os
import time
from decimal import Decimal
from statistics import median
from typing import Any

import aiohttp

from app import alerts, investigate
from app.schemas import MarketExample, PriceCheckItem, PriceCheckReport

SEARCH_URL = "https://api.trychannel3.com/v1/search"
TIMEOUT_SECONDS = 20
RESULTS_PER_PRODUCT = 20
CACHE_FILE = "channel3-cache.json"
CACHE_SECONDS = 6 * 60 * 60
UNDERPRICED = Decimal("0.5")  # of the 10th-percentile market price
OVERPRICED = Decimal("1.5")  # of the 90th-percentile market price
MIN_SAMPLES = 3
CENT = Decimal("0.01")


class Channel3Error(RuntimeError):
    """Channel3 refused or failed the search."""


def _key() -> str | None:
    return os.environ.get("TESTPILOT_CHANNEL3_API_KEY", "").strip().strip("\"'") or None


def configured() -> bool:
    return _key() is not None


async def search(query: str) -> list[dict[str, Any]]:
    """Comparable new, in-stock products for one query, from Channel3 (cached)."""
    cache = alerts._load(CACHE_FILE)
    hit = cache.get(query)
    if hit and time.time() - float(hit.get("at", 0)) < CACHE_SECONDS:
        return hit["products"]
    body = {"query": query, "limit": RESULTS_PER_PRODUCT,
            "filters": {"conditions": ["new"], "availability": ["InStock"]},
            "config": {"country": "US", "currency": "USD"}}
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(SEARCH_URL, json=body, headers={"x-api-key": _key() or ""}) as response:
                text = await response.text()
                status = response.status
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise Channel3Error("Channel3 could not be reached.") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}
    if status >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise Channel3Error(f"Channel3 refused the search (HTTP {status}{': ' + detail if isinstance(detail, str) else ''}).")
    products = data.get("products") if isinstance(data, dict) else None
    products = products if isinstance(products, list) else []
    cache[query] = {"at": time.time(), "products": products}
    alerts._save(CACHE_FILE, cache)
    return products


def _offers(products: list[dict[str, Any]]) -> list[tuple[Decimal, dict[str, Any], dict[str, Any]]]:
    """(price, product, offer) for every USD offer, cheapest offer per product."""
    found = []
    for product in products:
        prices = []
        for offer in product.get("offers") or []:
            price = (offer.get("price") or {})
            if price.get("currency") == "USD" and isinstance(price.get("price"), (int, float)) and price["price"] > 0:
                prices.append((Decimal(str(price["price"])), offer))
        if prices:
            amount, offer = min(prices, key=lambda pair: pair[0])
            found.append((amount, product, offer))
    return found


def judge(product_id: str, name: str, store_price: Decimal, query: str, products: list[dict[str, Any]]) -> PriceCheckItem:
    """Compare one store price with the market. Deterministic."""
    offers = _offers(products)
    base = {"product_id": product_id, "name": name, "store_price": str(store_price.quantize(CENT)),
            "market_query": query, "samples": len(offers)}
    if len(offers) < MIN_SAMPLES:
        return PriceCheckItem(**base, verdict="no_match",
                              message=f"Too few comparable products found for \"{query}\" to judge the price.")
    amounts = sorted(amount for amount, _, _ in offers)
    middle = Decimal(str(median(amounts))).quantize(CENT)
    cheap = amounts[int(0.1 * (len(amounts) - 1))]
    dear = amounts[int(0.9 * (len(amounts) - 1))]
    examples = [MarketExample(title=str(product.get("title", ""))[:120], price=str(amount.quantize(CENT)), currency="USD",
                              retailer=str(offer.get("domain", "")), url=str(offer.get("url", "")))
                for amount, product, offer in sorted(offers, key=lambda item: abs(item[0] - middle))[:3]]
    market = {"market_low": str(amounts[0].quantize(CENT)), "market_median": str(middle),
              "market_high": str(amounts[-1].quantize(CENT)), "examples": examples}
    if store_price < cheap * UNDERPRICED:
        verdict, message = "underpriced", (
            f"{name} sells for {store_price:.2f} USD, but comparable products sell for about {middle} USD. "
            f"If this is a typo, every sale loses about {(middle - store_price).quantize(CENT)} USD.")
    elif store_price > dear * OVERPRICED:
        verdict, message = "overpriced", (
            f"{name} sells for {store_price:.2f} USD, far above comparable products (most under {dear:.2f} USD), "
            "which can cost sales.")
    else:
        verdict, message = "ok", f"{name} at {store_price:.2f} USD is in line with the market (about {middle} USD)."
    return PriceCheckItem(**base, **market, verdict=verdict, message=message)


async def check(store: str) -> PriceCheckReport:
    """Every product the store sells, against the market."""
    if not configured():
        return PriceCheckReport(store=store, checked=False, note=(
            "Add a Channel3 API key (TESTPILOT_CHANNEL3_API_KEY) to compare prices with the market. Keys are free "
            "for hackathon participants at trychannel3.com/developers."))
    investigate._active_store.set(store)
    try:
        async with investigate._session() as session:
            _, listed = await investigate._store_json(session, "GET", "/api/products")
    except investigate.StoreUnavailable as exc:
        return PriceCheckReport(store=store, checked=False, note=str(exc))
    items = []
    try:
        for product in listed if isinstance(listed, list) else []:
            query = str(product.get("market") or product.get("name") or "").strip()
            price = investigate._decimal(product.get("price"))
            if not query or price is None:
                continue
            items.append(judge(str(product["id"]), str(product.get("name", product["id"])), price, query,
                               await search(query)))
    except Channel3Error as exc:
        return PriceCheckReport(store=store, checked=False, items=items, note=str(exc))
    return PriceCheckReport(store=store, checked=True, items=items)
