"""Market price check: Channel3 offers become a market price, and typo-like prices are flagged."""

import asyncio
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from app import price_check


def product(title, *prices, currency="USD", domain="shop.example"):
    return {"id": title, "title": title, "offers": [
        {"url": f"https://{domain}/{title}", "domain": domain, "price": {"price": p, "currency": currency}} for p in prices]}


MUGS = [product("Mug A", 11.0, 13.0), product("Mug B", 12.5), product("Mug C", 14.0), product("Mug D", 10.0),
        product("Mug E", 99.0, currency="EUR")]


class JudgeTests(unittest.TestCase):
    def test_a_price_far_below_the_market_is_a_likely_typo(self):
        item = price_check.judge("mug", "Evidence mug", Decimal("1.25"), "ceramic coffee mug", MUGS)
        self.assertEqual((item.verdict, item.samples, item.market_median), ("underpriced", 4, "11.75"))
        self.assertIn("every sale loses about 10.50 USD", item.message)
        self.assertEqual(len(item.examples), 3)

    def test_a_normal_price_is_ok_and_a_very_high_one_is_overpriced(self):
        self.assertEqual(price_check.judge("mug", "Mug", Decimal("12.50"), "q", MUGS).verdict, "ok")
        self.assertEqual(price_check.judge("mug", "Mug", Decimal("60.00"), "q", MUGS).verdict, "overpriced")

    def test_the_cheapest_usd_offer_counts_and_other_currencies_are_ignored(self):
        item = price_check.judge("mug", "Mug", Decimal("12.50"), "q", MUGS)
        self.assertEqual((item.market_low, item.market_high), ("10.00", "14.00"))

    def test_a_cheap_but_normal_price_in_a_wide_market_is_not_a_typo(self):
        stickers = [product(f"Pack {n}", price) for n, price in enumerate([3.0, 4.5, 6.0, 12, 15, 18, 18, 18, 25, 40])]
        self.assertEqual(price_check.judge("stickers", "Sticker pack", Decimal("4.99"), "q", stickers).verdict, "ok")

    def test_too_few_matches_are_not_judged(self):
        self.assertEqual(price_check.judge("mug", "Mug", Decimal("1.00"), "q", MUGS[:2]).verdict, "no_match")


class CheckTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {"TESTPILOT_ALERTS_DIR": folder.name, "TESTPILOT_CHANNEL3_API_KEY": "key"})
        env.start()
        self.addCleanup(env.stop)

    def run_check(self, searched=None):
        calls = []

        async def fake_store_json(_session, method, path, body=None):
            return 200, [{"id": "mug", "name": "Evidence mug", "price": "1.25", "market": "ceramic coffee mug 11 oz"},
                         {"id": "tee", "name": "T-shirt", "price": "18.00"}]

        async def fake_search(query):
            calls.append(query)
            if searched:
                raise searched
            return MUGS

        with patch("app.investigate._store_json", fake_store_json), patch("app.price_check.search", fake_search):
            return asyncio.run(price_check.check("vulnerable")), calls

    def test_each_product_is_searched_by_its_market_words(self):
        report, calls = self.run_check()
        self.assertEqual(calls, ["ceramic coffee mug 11 oz", "T-shirt"])
        self.assertEqual([i.verdict for i in report.items], ["underpriced", "ok"])

    def test_without_a_key_nothing_is_searched(self):
        with patch.dict(os.environ, {"TESTPILOT_CHANNEL3_API_KEY": ""}):
            report = asyncio.run(price_check.check("main"))
        self.assertFalse(report.checked)
        self.assertIn("trychannel3.com/developers", report.note)

    def test_channel3_refusing_is_reported_not_raised(self):
        report, _ = self.run_check(searched=price_check.Channel3Error("Channel3 refused the search (HTTP 402)."))
        self.assertFalse(report.checked)
        self.assertIn("HTTP 402", report.note)

    def test_searches_are_cached(self):
        sent = []

        class FakeResponse:
            status = 200

            async def text(self):
                return '{"products": [{"title": "Mug"}]}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeSession:
            def __init__(self, **_):
                pass

            def post(self, url, json=None, headers=None):
                sent.append((url, json, headers))
                return FakeResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        with patch("app.price_check.aiohttp.ClientSession", FakeSession):
            first = asyncio.run(price_check.search("mug"))
            second = asyncio.run(price_check.search("mug"))
        self.assertEqual(first, second)
        self.assertEqual(len(sent), 1)
        url, body, headers = sent[0]
        self.assertEqual((url, headers["x-api-key"]), ("https://api.trychannel3.com/v1/search", "key"))
        self.assertEqual(body["filters"], {"conditions": ["new"], "availability": ["InStock"]})


if __name__ == "__main__":
    unittest.main()
