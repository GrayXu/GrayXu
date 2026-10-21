import tempfile
import unittest
from datetime import date
from pathlib import Path

from token_heatmap.db import (
    aggregate_range,
    connect,
    fetch_daily_usage,
    replace_source_range,
    upsert_source_usage,
)
from token_heatmap.models import DailyUsage


class AggregateTests(unittest.TestCase):
    def test_snapshots_keep_per_machine_maxima_without_adding_retries(self):
        usage_date = date(2026, 8, 21)
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "usage.sqlite")
            for usage in (
                DailyUsage(
                    usage_date,
                    "ccusage",
                    "mac",
                    input_tokens=80,
                    output_tokens=20,
                    total_tokens=100,
                    cost_usd=1.0,
                ),
                DailyUsage(
                    usage_date,
                    "ccusage",
                    "mac",
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    cost_usd=0.1,
                ),
                DailyUsage(usage_date, "ccusage", "mac"),
                DailyUsage(
                    usage_date,
                    "ccusage",
                    "mac",
                    input_tokens=90,
                    output_tokens=20,
                    total_tokens=110,
                    cost_usd=1.1,
                ),
                DailyUsage(
                    usage_date,
                    "ccusage",
                    "mac",
                    input_tokens=90,
                    output_tokens=20,
                    total_tokens=110,
                    cost_usd=1.1,
                ),
                DailyUsage(usage_date, "ccusage", "nas", total_tokens=30),
            ):
                upsert_source_usage(connection, [usage])
            aggregate_range(connection, usage_date, usage_date)
            row = fetch_daily_usage(connection, usage_date, usage_date)[0]
            self.assertEqual(row.ccusage_tokens, 140)
            self.assertEqual(row.input_tokens, 90)
            self.assertEqual(row.output_tokens, 20)
            self.assertEqual(row.cost_usd, 1.1)
            connection.close()

    def test_aggregation_preserves_published_total_when_source_history_shrinks(self):
        usage_date = date(2026, 8, 21)
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "usage.sqlite")
            upsert_source_usage(
                connection, [DailyUsage(usage_date, "ccusage", "mac", total_tokens=100)]
            )
            replace_source_range(
                connection,
                "cpa",
                "",
                usage_date,
                usage_date,
                [DailyUsage(usage_date, "cpa", total_tokens=200)],
            )
            aggregate_range(connection, usage_date, usage_date)
            replace_source_range(connection, "cpa", "", usage_date, usage_date, [])
            aggregate_range(connection, usage_date, usage_date)
            row = fetch_daily_usage(connection, usage_date, usage_date)[0]
            self.assertEqual(row.total_tokens, 300)
            self.assertEqual(row.ccusage_tokens, 100)
            self.assertEqual(row.cpa_tokens, 0)
            upsert_source_usage(
                connection, [DailyUsage(usage_date, "ccusage", "mac", total_tokens=400)]
            )
            aggregate_range(connection, usage_date, usage_date)
            self.assertEqual(
                fetch_daily_usage(connection, usage_date, usage_date)[0].total_tokens,
                400,
            )
            connection.close()

    def test_ccusage_and_cpa_are_added_with_breakdown(self):
        usage_date = date(2026, 8, 21)
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "usage.sqlite")
            upsert_source_usage(
                connection,
                [
                    DailyUsage(
                        usage_date,
                        "ccusage",
                        "gray-mac",
                        input_tokens=10,
                        cached_input_tokens=80,
                        output_tokens=10,
                        total_tokens=100,
                    ),
                    DailyUsage(
                        usage_date,
                        "cpa",
                        "",
                        input_tokens=20,
                        cached_input_tokens=160,
                        output_tokens=20,
                        total_tokens=200,
                        request_count=2,
                    ),
                ],
            )
            aggregate_range(connection, usage_date, usage_date)
            row = fetch_daily_usage(connection, usage_date, usage_date)[0]
            connection.close()
        self.assertEqual(row.ccusage_tokens, 100)
        self.assertEqual(row.cpa_tokens, 200)
        self.assertEqual(row.total_tokens, 300)
        self.assertEqual(row.input_tokens, 30)
        self.assertEqual(row.cached_input_tokens, 240)

    def test_replace_source_range_removes_stale_snapshot(self):
        usage_date = date(2026, 8, 21)
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "usage.sqlite")
            replace_source_range(
                connection,
                "cpa",
                "",
                usage_date,
                usage_date,
                [DailyUsage(usage_date, "cpa", total_tokens=200)],
            )
            replace_source_range(
                connection, "cpa", "", usage_date, usage_date, []
            )
            aggregate_range(connection, usage_date, usage_date)
            rows = fetch_daily_usage(connection, usage_date, usage_date)
            connection.close()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
