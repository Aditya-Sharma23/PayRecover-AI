import asyncio
import socket
import unittest

from app.website_runner import BROWSER_LOCKDOWN_ARGS, PinnedResolver, _same_origin


class RunnerHelperTests(unittest.TestCase):
    def test_same_origin_includes_scheme_and_effective_port(self):
        self.assertTrue(_same_origin("https://example.com/", "https://example.com/path"))
        self.assertTrue(_same_origin("http://example.com/", "http://example.com:80/path"))
        self.assertFalse(_same_origin("http://example.com/", "https://example.com/"))
        self.assertFalse(_same_origin("https://example.com/", "https://example.com:444/"))
        self.assertFalse(_same_origin("https://example.com/", "https://other.example/"))

    def test_pinned_resolver_only_returns_validated_addresses_for_the_validated_host(self):
        resolver = PinnedResolver("Example.com.", ("93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"))
        answers = asyncio.run(resolver.resolve("example.com", 443))
        self.assertEqual([answer["host"] for answer in answers], list(resolver.addresses))
        self.assertEqual(answers[1]["family"], socket.AF_INET6)
        with self.assertRaises(OSError):
            asyncio.run(resolver.resolve("rebound.example", 443))

    def test_browser_has_no_network_path_of_its_own(self):
        self.assertIn("--host-resolver-rules=MAP * ~NOTFOUND", BROWSER_LOCKDOWN_ARGS)
        self.assertTrue(any(arg.startswith("--proxy-server=") for arg in BROWSER_LOCKDOWN_ARGS))
        self.assertTrue(any("disable_non_proxied_udp" in arg for arg in BROWSER_LOCKDOWN_ARGS))


if __name__ == "__main__":
    unittest.main()
