import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from token_heatmap.app import (
    build_days,
    copy_snapshot,
    filtered_codex_home,
    load_config,
    merge_days,
    run_ccusage,
)


class AppTests(unittest.TestCase):
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
