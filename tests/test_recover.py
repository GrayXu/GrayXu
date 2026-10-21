import json
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from token_heatmap.data import build_data_payload
from token_heatmap.db import (
    aggregate_range,
    connect,
    fetch_daily_usage,
    upsert_source_usage,
)
from token_heatmap.models import AggregatedUsage, DailyUsage
from token_heatmap.recover import history_maxima, restore_totals


class RecoveryTests(unittest.TestCase):
    def test_history_uses_maximum_and_respects_accounting_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def git(*arguments):
                return subprocess.check_output(
                    ["git", "-C", str(repo), *arguments], text=True
                ).strip()

            git("init", "-q")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            day = date(2026, 10, 1)
            commits = []
            for index, total in enumerate((1000, 200, 50, 300, 0)):
                usage = AggregatedUsage(day, 0, 0, 0, 0, 0, 0, total, 0, 0.0)
                payload = build_data_payload([usage], "Asia/Shanghai", day)
                (repo / "daily_usage.json").write_text(json.dumps(payload))
                git("add", "daily_usage.json")
                git("commit", "-q", "-m", f"snapshot {index}")
                commits.append(git("rev-parse", "HEAD"))
            maxima, count = history_maxima(repo, "HEAD")
            self.assertEqual(count, 5)
            self.assertEqual(maxima[day], (1000, commits[0]))
            maxima, count = history_maxima(repo, "HEAD", commits[1])
            self.assertEqual(count, 4)
            self.assertEqual(maxima[day], (300, commits[3]))

    def test_recovery_is_read_only_by_default_and_survives_reaggregation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "usage.sqlite"
            day = date(2026, 10, 1)
            connection = connect(database)
            upsert_source_usage(
                connection, [DailyUsage(day, "ccusage", "mac", total_tokens=40)]
            )
            aggregate_range(connection, day, day)
            connection.close()
            maxima = {day: (100, "commit")}
            self.assertEqual(restore_totals(database, maxima)["total_added_tokens"], 60)
            connection = connect(database)
            self.assertEqual(
                fetch_daily_usage(connection, day, day)[0].total_tokens, 40
            )
            connection.close()
            self.assertEqual(
                restore_totals(database, maxima, apply=True)["changed_dates"], 1
            )
            self.assertEqual(
                restore_totals(database, maxima, apply=True)["changed_dates"], 0
            )
            connection = connect(database)
            aggregate_range(connection, day, day)
            usage = fetch_daily_usage(connection, day, day)[0]
            self.assertEqual(usage.total_tokens, 100)
            self.assertEqual(usage.ccusage_tokens, 40)
            self.assertEqual(usage.cpa_tokens, 0)
            upsert_source_usage(
                connection, [DailyUsage(day, "ccusage", "mac", total_tokens=150)]
            )
            aggregate_range(connection, day, day)
            self.assertEqual(
                fetch_daily_usage(connection, day, day)[0].total_tokens, 150
            )
            connection.close()

    def test_recovery_validates_all_rows_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "absent.sqlite"
            with self.assertRaises(ValueError):
                restore_totals(
                    database, {date(2026, 10, 1): (-1, "commit")}, apply=True
                )
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
