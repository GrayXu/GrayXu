import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from token_heatmap.cpa import CPAAdapter


SCHEMA = """
CREATE TABLE usage_events (
    timestamp_ms INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    normalized_uncached_input_tokens INTEGER,
    normalized_cache_read_tokens INTEGER
)
"""


def epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).astimezone(timezone.utc).timestamp() * 1000)


class CPAAdapterTests(unittest.TestCase):
    def test_groups_utc_rows_by_asia_shanghai_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cpa.sqlite"
            connection = sqlite3.connect(path)
            connection.execute(SCHEMA)
            connection.executemany(
                "INSERT INTO usage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (epoch_ms("2026-08-20T15:59:59+00:00"), 90, 10, 3, 80, 100, 10, 80),
                    (epoch_ms("2026-08-20T16:00:00+00:00"), 180, 20, 8, 150, 200, 30, 150),
                ],
            )
            connection.commit()
            connection.close()

            rows = CPAAdapter(path, "Asia/Shanghai").get_daily_usage(
                date(2026, 8, 20), date(2026, 8, 21)
            )

        self.assertEqual(rows[0].date, date(2026, 8, 20))
        self.assertEqual(rows[0].total_tokens, 100)
        self.assertEqual(rows[0].input_tokens, 10)
        self.assertEqual(rows[1].date, date(2026, 8, 21))
        self.assertEqual(rows[1].total_tokens, 200)
        self.assertEqual(rows[1].cached_input_tokens, 150)
        self.assertEqual(rows[1].request_count, 1)

    def test_missing_database_is_read_only_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite"
            with self.assertRaises(sqlite3.OperationalError):
                CPAAdapter(path, "Asia/Shanghai").get_daily_usage(
                    date(2026, 8, 20), date(2026, 8, 21)
                )
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
