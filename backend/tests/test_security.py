import os
import unittest
from unittest.mock import patch

from app.security import RequestLimiter, _valid_token, authenticate, configured_tokens


class SecurityTests(unittest.TestCase):
    def test_limiter_expires_requests_after_window(self):
        limiter = RequestLimiter(limit=2, window_seconds=10)
        self.assertTrue(limiter.allow("client", now=1))
        self.assertTrue(limiter.allow("client", now=2))
        self.assertFalse(limiter.allow("client", now=3))
        self.assertTrue(limiter.allow("client", now=12.1))

    def test_bearer_token_is_checked_without_exposing_secret(self):
        class Client:
            host = "127.0.0.1"

        class Url:
            path = "/api/tests/website"

        class Request:
            url = Url()
            client = Client()
            headers = {"authorization": "Bearer local-secret"}

        with patch.dict(os.environ, {"TESTPILOT_API_TOKEN": "local-secret"}, clear=True):
            self.assertTrue(_valid_token(Request()))
            Request.headers = {"authorization": "Bearer wrong"}
            self.assertFalse(_valid_token(Request()))

    def test_each_configured_token_maps_to_its_own_owner(self):
        class Request:
            headers = {"x-testpilot-api-key": "bob-token"}

        values = {"TESTPILOT_API_TOKENS": "alice:alice-token, bob:bob-token,bad name:x,:nope"}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(configured_tokens(), {"alice-token": "alice", "bob-token": "bob"})
            self.assertEqual(authenticate(Request()), "bob")
            Request.headers = {"authorization": "Bearer alice-token"}
            self.assertEqual(authenticate(Request()), "alice")
            Request.headers = {}
            self.assertIsNone(authenticate(Request()))


if __name__ == "__main__":
    unittest.main()
