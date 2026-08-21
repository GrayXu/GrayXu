import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from token_heatmap.data import build_data_payload, load_data_file
from token_heatmap.heatmap import (
    DISPLAY_DAYS,
    END_MARKER,
    START_MARKER,
    update_readme,
)
from token_heatmap.models import AggregatedUsage


def usage(usage_date, total_tokens):
    return AggregatedUsage(
        date=usage_date,
        ccusage_tokens=11,
        cpa_tokens=22,
        input_tokens=1,
        cached_input_tokens=2,
        output_tokens=3,
        reasoning_tokens=1,
        total_tokens=total_tokens,
        request_count=4,
        cost_usd=5.0,
    )


class Titles(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        if tag == "img" and dict(attrs).get("title"):
            self.values.append(dict(attrs)["title"])


class DataFileTests(unittest.TestCase):
    def test_payload_contains_only_sixty_days_of_total_tokens(self):
        end = date(2026, 8, 21)
        payload = build_data_payload(
            [usage(end, 123)],
            "Asia/Shanghai",
            end,
            generated_at=datetime(2026, 8, 21, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(len(payload["days"]), DISPLAY_DAYS)
        self.assertEqual(
            payload["start_date"],
            (end - timedelta(days=DISPLAY_DAYS - 1)).isoformat(),
        )
        self.assertEqual(payload["days"][-1], {"date": "2026-08-21", "total_tokens": 123})
        self.assertEqual(set(payload["days"][-1]), {"date", "total_tokens"})
        self.assertNotIn("ccusage", json.dumps(payload))
        self.assertNotIn("cpa", json.dumps(payload).lower())

    def test_loader_rejects_non_continuous_days(self):
        end = date(2026, 8, 21)
        payload = build_data_payload([], "Asia/Shanghai", end)
        payload["days"][1]["date"] = payload["days"][0]["date"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily_usage.json"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "continuous"):
                load_data_file(path)

    def test_exported_data_renders_total_only_tooltips(self):
        end = date(2026, 8, 21)
        payload = build_data_payload([usage(end, 123)], "Asia/Shanghai", end)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_path = root / "daily_usage.json"
            data_path.write_text(json.dumps(payload))
            usage_data = load_data_file(data_path)
            readme = root / "README.md"
            readme.write_text(f"{START_MARKER}\nold\n{END_MARKER}\n")
            update_readme(
                readme,
                root / "assets",
                usage_data.days,
                usage_data.days[0].date,
                usage_data.days[-1].date,
            )
            parser = Titles()
            parser.feed(readme.read_text())
        self.assertEqual(len(parser.values), DISPLAY_DAYS)
        self.assertIn("Aug 21, 2026 · 123 tokens", parser.values)
        self.assertTrue(all("ccusage" not in title for title in parser.values))
        self.assertTrue(all("CPA" not in title for title in parser.values))


if __name__ == "__main__":
    unittest.main()
