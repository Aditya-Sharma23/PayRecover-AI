"""Reflected-XSS probe: payload classification and per-payload outcomes."""

import asyncio
import html
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, unquote, urlsplit

from app import security_checks, xss_probe

TARGET = "https://fixture.test/"


def page(echo):
    """A fake site: echo(params, path) returns the HTML body for one GET."""
    async def fetch(url, method="GET", body_limit=0):
        parts = urlsplit(url)
        body = echo(dict(parse_qsl(parts.query)), unquote(parts.path))
        return security_checks.ProbeResponse(200, {"content-type": "text/html; charset=utf-8"}, [], body.encode())
    return fetch


def scan(echo):
    with patch("app.security_checks.fetch_probe", page(echo)):
        return asyncio.run(xss_probe.scan(TARGET))


def outcomes(result):
    return {item["id"]: item["outcome"] for item in result["payloads"]}


class ClassifyTests(unittest.TestCase):
    def test_outcomes(self):
        token, signature = "tpxab", "<tpxab>"
        self.assertEqual(xss_probe.classify("<p><tpxab></p>", "text/html", token, signature), "unescaped")
        self.assertEqual(xss_probe.classify("<p>&lt;tpxab&gt;</p>", "text/html", token, signature), "encoded")
        self.assertEqual(xss_probe.classify('{"q": "<tpxab>"}', "application/json", token, signature), "non_html")
        self.assertEqual(xss_probe.classify("<p>hello</p>", "text/html", token, signature), "not_reflected")

    def test_a_javascript_url_counts_only_inside_a_url_attribute(self):
        signature = r"re:\b(?:href|src|action|formaction|data)\s*=\s*[\"']?\s*javascript:tpxab"
        self.assertEqual(xss_probe.classify("<p>javascript:tpxab</p>", "text/html", "tpxab", signature), "encoded")
        self.assertEqual(
            xss_probe.classify('<a href="javascript:tpxab">go</a>', "text/html", "tpxab", signature), "unescaped"
        )


class ScanTests(unittest.TestCase):
    def test_a_page_that_echoes_nothing_passes(self):
        result = scan(lambda params, path: "<h1>Shop</h1>")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(set(outcomes(result).values()), {"not_reflected"})
        self.assertEqual(result["payload_count"], len(xss_probe.PAYLOADS))
        self.assertGreaterEqual(len(xss_probe.PAYLOADS), 15)
        self.assertEqual(result["requests"], len(xss_probe.PAYLOADS) + len(xss_probe.PATH_PAYLOADS))

    def test_an_escaping_search_box_is_safe(self):
        result = scan(lambda params, path: f"<p>Results for {html.escape(params.get('search', ''))}</p>")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(set(outcomes(result).values()), {"encoded"})
        self.assertEqual(result["findings"], [])

    def test_a_raw_search_box_is_xss_and_names_the_parameter(self):
        result = scan(lambda params, path: f"<p>Results for {params.get('search', '')}</p>")
        self.assertEqual(result["status"], "failed")
        found = {item["id"]: item for item in result["payloads"]}
        self.assertEqual(found["script_tag"]["outcome"], "unescaped")
        self.assertEqual(found["script_tag"]["reflected_in"], ["search"])
        self.assertTrue(any(f["title"] == "Reflected XSS: Script element" for f in result["findings"]))

    def test_an_error_page_that_echoes_the_path_is_caught(self):
        result = scan(lambda params, path: f"<p>Not found: {path}</p>")
        found = {item["id"]: item for item in result["payloads"]}
        self.assertEqual(found["html_tag"]["outcome"], "unescaped")
        self.assertEqual(found["html_tag"]["reflected_in"], ["(URL path)"])

    def test_payloads_never_name_a_real_function(self):
        for _, _, _, _, payload, _ in xss_probe.PAYLOADS:
            text = payload.format(t="tpx00").lower()
            for danger in ("alert(", "fetch(", "document.cookie", "eval(", "http://", "https://"):
                self.assertNotIn(danger, text)


if __name__ == "__main__":
    unittest.main()
