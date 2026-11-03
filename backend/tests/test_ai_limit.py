"""The daily AI limit: requests are counted per UTC day, and over the limit callers get their built-in answers."""

import isolation
import os
import unittest
from unittest.mock import patch

from app import ai

DAY = 1793750400.0  # 2026-11-03 00:00 UTC


def setUpModule():
    isolation.isolate_alerts()


def tearDownModule():
    isolation.restore_alerts()


class DailyLimitTests(unittest.TestCase):
    def test_requests_are_counted_and_refused_past_the_limit(self):
        with patch.dict(os.environ, {"TESTPILOT_AI_DAILY_LIMIT": "2"}):
            ai._spend_one(DAY + 10)
            ai._spend_one(DAY + 20)
            self.assertEqual(ai.usage_today(DAY + 30), 2)
            with self.assertRaises(ai.AIProviderError) as raised:
                ai._spend_one(DAY + 30)
        self.assertIn("limit", str(raised.exception))

    def test_the_count_starts_again_the_next_day(self):
        with patch.dict(os.environ, {"TESTPILOT_AI_DAILY_LIMIT": "1"}):
            ai._spend_one(DAY + 86400 * 5)
            ai._spend_one(DAY + 86400 * 6)
            self.assertEqual(ai.usage_today(DAY + 86400 * 6), 1)

    def test_zero_means_no_limit(self):
        with patch.dict(os.environ, {"TESTPILOT_AI_DAILY_LIMIT": "0"}):
            for _ in range(5):
                ai._spend_one(DAY + 86400 * 9)


if __name__ == "__main__":
    unittest.main()
