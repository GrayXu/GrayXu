import unittest
from datetime import date
from unittest.mock import patch

from client.upload_ccusage import build_days, copy_snapshot


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

    @patch("client.upload_ccusage.subprocess.run")
    def test_copy_snapshot_uses_scp_then_atomic_remote_move(self, run):
        payload = {"machine_id": "gray-mac", "days": [{"date": "2026-08-21"}]}
        self.assertEqual(
            copy_snapshot("ali", "/var/lib/token-heatmap/inbox", payload), 1
        )
        scp, ssh = run.call_args_list
        self.assertEqual(scp.args[0][0:2], ["/usr/bin/scp", "-q"])
        self.assertRegex(
            scp.args[0][-1],
            r"^ali:/var/lib/token-heatmap/inbox/\.gray-mac\.\d+\.json\.tmp$",
        )
        self.assertEqual(ssh.args[0][0:2], ["/usr/bin/ssh", "ali"])
        self.assertIn("chown root:token-heatmap", ssh.args[0][2])
        self.assertIn("mv -f", ssh.args[0][2])


if __name__ == "__main__":
    unittest.main()
