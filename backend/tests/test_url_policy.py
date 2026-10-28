import asyncio
import unittest

from app.url_policy import UnsafeTarget, validate_target_url


class UrlPolicyTests(unittest.TestCase):
    def assertUnsafe(self, url: str) -> None:
        with self.assertRaises(UnsafeTarget):
            asyncio.run(validate_target_url(url))

    def test_rejects_local_and_private_targets(self):
        self.assertUnsafe("http://localhost/")
        self.assertUnsafe("http://127.0.0.1/")
        self.assertUnsafe("http://192.168.1.10/")

    def test_rejects_metadata_link_local_ipv6_and_unsupported_schemes(self):
        self.assertUnsafe("http://169.254.169.254/latest/meta-data/")
        self.assertUnsafe("http://10.0.0.1/")
        self.assertUnsafe("http://0.0.0.0/")
        self.assertUnsafe("http://[::1]/")
        self.assertUnsafe("http://[fe80::1]/")
        self.assertUnsafe("file:///etc/passwd")
        self.assertUnsafe("ftp://example.com/")
        self.assertUnsafe("http://app.localhost/")

    def test_rejects_credentials_and_fragments(self):
        self.assertUnsafe("https://user:password@example.com/")
        self.assertUnsafe("https://example.com/#private")

    def test_accepts_public_literal_ip(self):
        self.assertEqual(
            asyncio.run(validate_target_url("https://93.184.216.34/")),
            ("93.184.216.34",),
        )


if __name__ == "__main__":
    unittest.main()
