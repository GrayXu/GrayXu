import unittest
from datetime import date

from client.upload_ccusage import build_days


class ClientTests(unittest.TestCase):
    def test_build_days_normalizes_ccusage_and_fills_missing_dates(self):
        report = {
            "daily": [
                {
                    "date": "2026-08-20",
                    "inputTokens": 10,
                    "cacheReadTokens": 80,
                    "outputTokens": 10,
                    "reasoningOutputTokens": 4,
                    "totalTokens": 100,
                    "costUSD": 0.5,
                }
            ]
        }
        days = build_days(report, date(2026, 8, 20), date(2026, 8, 21))
        self.assertEqual(days[0]["cached_input_tokens"], 80)
        self.assertEqual(days[0]["reasoning_tokens"], 4)
        self.assertEqual(days[0]["total_tokens"], 100)
        self.assertEqual(days[1]["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
