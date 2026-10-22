import configparser
import json
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from token_heatmap.app import (
    build_days,
    copy_snapshot,
    filtered_codex_home,
    load_config,
    merge_days,
    run_ccusage,
    sync_primary,
    sync_sender,
)
from token_heatmap.models import AggregatedUsage


class AppTests(unittest.TestCase):
    def test_primary_publishes_only_completed_days_in_configured_timezone(self):
        cases = (
            ("2026-10-21T15:59:00+00:00", date(2026, 10, 20)),
            ("2026-10-21T16:01:00+00:00", date(2026, 10, 21)),
            ("2026-03-31T16:00:00+00:00", date(2026, 3, 31)),
            ("2026-12-31T16:00:00+00:00", date(2026, 12, 31)),
            ("2024-02-29T16:00:00+00:00", date(2024, 2, 29)),
        )
        for instant, end_date in cases:
            with self.subTest(instant=instant), TemporaryDirectory() as directory:
                root = Path(directory)
                config = configparser.ConfigParser()
                config.read_dict(
                    {
                        "app": {"timezone": "Asia/Shanghai"},
                        "primary": {
                            "database": str(root / "usage.sqlite"),
                            "inbox": str(root / "inbox"),
                            "data_repo": str(root),
                        },
                    }
                )
                totals = [
                    AggregatedUsage(day, 0, 0, 0, 0, 0, 0, total, 0, 0.0)
                    for day, total in (
                        (end_date, 42),
                        (end_date + timedelta(days=1), 9999),
                    )
                ]
                frozen = datetime.fromisoformat(instant)
                with (
                    patch("token_heatmap.app.datetime") as clock,
                    patch("token_heatmap.app.sync_branch"),
                    patch("token_heatmap.app.import_ccusage_snapshots"),
                    patch(
                        "token_heatmap.app.refresh_daily_usage", return_value=totals
                    ) as refresh,
                    patch(
                        "token_heatmap.app.git",
                        return_value=subprocess.CompletedProcess([], 0, stdout="0"),
                    ),
                ):
                    clock.now.side_effect = lambda zone: frozen.astimezone(zone)
                    sync_primary(config)
                self.assertEqual(refresh.call_args.args[-1], end_date)
                payload = json.loads((root / "daily_usage.json").read_text())
                self.assertEqual(payload["end_date"], end_date.isoformat())
                self.assertEqual(
                    payload["start_date"], (end_date - timedelta(days=59)).isoformat()
                )
                self.assertEqual(
                    payload["days"][-1],
                    {"date": end_date.isoformat(), "total_tokens": 42},
                )
                self.assertEqual(len(payload["days"]), 60)
                self.assertTrue(
                    all(
                        date.fromisoformat(row["date"]) <= end_date
                        for row in payload["days"]
                    )
                )

    def test_sender_reports_three_completed_days_without_today(self):
        for instant, end_date in (
            ("2026-10-21T15:59:00+00:00", date(2026, 10, 20)),
            ("2026-10-21T16:01:00+00:00", date(2026, 10, 21)),
        ):
            with self.subTest(instant=instant):
                config = configparser.ConfigParser()
                config.read_dict(
                    {
                        "app": {"timezone": "Asia/Shanghai"},
                        "sender": {
                            "ccusage_command": sys.executable,
                            "agents": "codex",
                            "days": "3",
                            "machine_id": "mac",
                            "ssh_host": "primary",
                            "remote_inbox": "/inbox",
                        },
                    }
                )
                report = {
                    "daily": [
                        {"date": end_date.isoformat(), "totalTokens": 42},
                        {
                            "date": (end_date + timedelta(days=1)).isoformat(),
                            "totalTokens": 9999,
                        },
                    ]
                }
                frozen = datetime.fromisoformat(instant)
                with (
                    patch("token_heatmap.app.datetime") as clock,
                    patch("token_heatmap.app.run_ccusage", return_value=report) as run,
                    patch("token_heatmap.app.copy_snapshot") as copy,
                ):
                    clock.now.side_effect = lambda zone: frozen.astimezone(zone)
                    sync_sender(config)
                self.assertEqual(
                    run.call_args.args[3:5], (end_date - timedelta(days=2), end_date)
                )
                payload = copy.call_args.args[2]
                self.assertEqual(len(payload["days"]), 3)
                self.assertEqual(payload["days"][-1]["date"], end_date.isoformat())
                self.assertEqual(payload["days"][-1]["total_tokens"], 42)
                self.assertEqual(
                    payload["generated_at"],
                    frozen.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
                )

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

    def test_merge_days_combines_agent_reports(self):
        reports = [
            {"daily": [{"date": "2026-08-20", "inputTokens": 10, "totalTokens": 10}]},
            {
                "daily": [
                    {
                        "date": "2026-08-20",
                        "outputTokens": 5,
                        "totalTokens": 5,
                        "totalCost": 0.25,
                    }
                ]
            },
        ]
        days = merge_days(reports, date(2026, 8, 20), date(2026, 8, 20))
        self.assertEqual(days[0]["input_tokens"], 10)
        self.assertEqual(days[0]["output_tokens"], 5)
        self.assertEqual(days[0]["total_tokens"], 15)
        self.assertEqual(days[0]["cost_usd"], 0.25)

    @patch("token_heatmap.app.subprocess.run")
    def test_run_ccusage_only_uses_fast_mode_for_codex(self, run):
        run.return_value.stdout = '{"daily": []}'
        command = ["bunx", "ccusage"]
        run_ccusage("codex", command, "Asia/Shanghai", date(2026, 8, 20), date(2026, 8, 20))
        run_ccusage("opencode", command, "Asia/Shanghai", date(2026, 8, 20), date(2026, 8, 20))
        self.assertIn("--speed", run.call_args_list[0].args[0])
        self.assertNotIn("--speed", run.call_args_list[1].args[0])

    def test_filtered_codex_home_excludes_cpa_sessions(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            sessions = source / "sessions" / "2026" / "08"
            sessions.mkdir(parents=True)
            (sessions / "cpa.jsonl").write_text(
                '{"type":"session_meta","payload":{"model_provider":"cpa"}}\n'
            )
            (sessions / "openai.jsonl").write_text(
                '{"type":"session_meta","payload":{"model_provider":"openai"}}\n'
            )
            with patch.dict("token_heatmap.app.os.environ", {"CODEX_HOME": str(source)}):
                with filtered_codex_home({"cpa"}) as filtered:
                    files = sorted(path.name for path in Path(filtered).rglob("*.jsonl"))
            self.assertEqual(files, ["openai.jsonl"])

    @patch("token_heatmap.app.subprocess.run")
    def test_copy_snapshot_uses_scp_then_atomic_remote_move(self, run):
        payload = {"machine_id": "gray-mac", "days": [{"date": "2026-08-21"}]}
        copy_snapshot("ali", "/var/lib/token-heatmap/inbox", payload)
        scp, ssh = run.call_args_list
        self.assertEqual(scp.args[0][0:2], ["/usr/bin/scp", "-q"])
        self.assertRegex(
            scp.args[0][-1],
            r"^ali:/var/lib/token-heatmap/inbox/\.gray-mac\.\d+\.json\.tmp$",
        )
        self.assertEqual(ssh.args[0][0:2], ["/usr/bin/ssh", "ali"])
        self.assertIn("chown root:token-heatmap", ssh.args[0][2])
        self.assertIn("mv -f", ssh.args[0][2])

    def test_example_config_has_both_roles_and_no_secret_fields(self):
        path = Path(__file__).parents[1] / "config.example.ini"
        config = load_config(path)
        self.assertFalse(config.getboolean("app", "primary"))
        self.assertTrue(config.has_section("sender"))
        self.assertTrue(config.has_section("primary"))
        text = path.read_text().lower()
        for forbidden in ("password", "private_key", "api_key", "token ="):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
