from html.parser import HTMLParser
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from token_heatmap.heatmap import (
    END_MARKER,
    DISPLAY_DAYS,
    LEVEL_COLORS,
    START_MARKER,
    STATS_HEIGHT,
    STATS_WIDTH,
    USAGE_LEVELS,
    format_tokens,
    percentile_thresholds,
    render_heatmap,
    render_month_labels,
    render_section,
    render_stats_svg,
    replace_marked_section,
    update_readme,
    usage_level,
    write_static_assets,
)
from token_heatmap.models import AggregatedUsage


class ImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))


def usage(usage_date, total=0, ccusage=0, cpa=0):
    return AggregatedUsage(
        date=usage_date,
        ccusage_tokens=ccusage,
        cpa_tokens=cpa,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        total_tokens=total,
        request_count=0,
        cost_usd=0.0,
    )


class HeatmapTests(unittest.TestCase):
    def test_higher_levels_use_darker_green(self):
        self.assertEqual(
            [LEVEL_COLORS[level] for level in range(1, USAGE_LEVELS + 1)],
            [
                "#9be9a8",
                "#77d98a",
                "#4dc86b",
                "#3bb85c",
                "#34a953",
                "#2d9549",
                "#267f40",
                "#1f6937",
                "#165630",
                "#0e4429",
            ],
        )

    def test_summary_uses_today_rolling_seven_days_and_month(self):
        end = date(2026, 8, 21)
        start = end - timedelta(days=364)
        section = render_section(
            [
                usage(date(2026, 8, 1), total=40),
                usage(date(2026, 8, 16), total=10),
                usage(date(2026, 8, 17), total=20),
                usage(end, total=30),
            ],
            start,
            end,
        )
        self.assertIn(
            'alt="30 today; 60 this week; 100 this month"', section
        )
        self.assertRegex(section, r'assets/heatmap/stats-[0-9a-f]{12}\.svg')
        self.assertNotIn("last 365 days", section)

    def test_stats_svg_has_three_vertical_summary_rows(self):
        end = date(2026, 8, 21)
        svg = render_stats_svg(
            [usage(end, total=30), usage(date(2026, 8, 17), total=20)], end
        )
        self.assertIn(f'width="{STATS_WIDTH}"', svg)
        self.assertIn(f'height="{STATS_HEIGHT}"', svg)
        self.assertIn('<text x="0" y="30">30 today</text>', svg)
        self.assertIn('<text x="0" y="78">50 this week</text>', svg)
        self.assertIn('<text x="0" y="126">50 this month</text>', svg)

    def test_static_assets_use_sixteen_pixel_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            asset_directory = Path(directory)
            write_static_assets(asset_directory)
            level = (asset_directory / "level-10.svg").read_text()
            blank = (asset_directory / "blank.svg").read_text()
            gap = (asset_directory / "gap.svg").read_text()
        self.assertIn('width="16" height="16"', level)
        self.assertIn('width="16" height="16"', blank)
        self.assertIn('width="2" height="16"', gap)

    def test_update_keeps_only_current_content_addressed_stats_asset(self):
        end = date(2026, 8, 21)
        start = end - timedelta(days=DISPLAY_DAYS - 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "assets"
            assets.mkdir()
            (assets / "stats.svg").write_text("stale")
            (assets / "stats-old.svg").write_text("stale")
            readme = root / "README.md"
            readme.write_text(f"{START_MARKER}\nold\n{END_MARKER}\n")
            update_readme(
                readme,
                assets,
                [usage(end, total=30)],
                start,
                end,
            )
            stats_assets = sorted(path.name for path in assets.glob("stats*.svg"))
            updated_readme = readme.read_text()
        self.assertEqual(len(stats_assets), 1)
        self.assertRegex(stats_assets[0], r"^stats-[0-9a-f]{12}\.svg$")
        self.assertIn(stats_assets[0], updated_readme)

    def test_format_tokens_preserves_significant_integer_zeroes(self):
        self.assertEqual(format_tokens(130_000_000), "130M")
        self.assertEqual(format_tokens(100_000_000_000), "100B")
        self.assertEqual(format_tokens(1_500_000), "1.5M")

    def test_percentiles_resist_outlier(self):
        thresholds = percentile_thresholds([*range(1, 11), 1_000_000])
        self.assertEqual(thresholds, tuple(range(2, 11)))
        self.assertEqual(usage_level(2, thresholds), 1)
        self.assertEqual(usage_level(10, thresholds), 9)
        self.assertEqual(usage_level(1_000_000, thresholds), 10)

    def test_render_has_365_titled_days_and_shared_assets(self):
        end = date(2026, 8, 21)
        start = end - timedelta(days=364)
        html = render_heatmap(
            [usage(start, total=3, ccusage=1, cpa=2), usage(end)], start, end
        )
        parser = ImageParser()
        parser.feed(html)
        titled = [image for image in parser.images if image.get("title")]
        gaps = [image for image in parser.images if image.get("src", "").endswith("gap.svg")]
        blanks = [image for image in parser.images if image.get("src", "").endswith("blank.svg")]
        levels = {
            image["src"] for image in parser.images if "level-" in image.get("src", "")
        }
        self.assertEqual(len(titled), 365)
        self.assertEqual(len(gaps), 52 * 7)
        self.assertEqual(len(blanks), 6)
        self.assertLessEqual(len(levels), USAGE_LEVELS)
        self.assertTrue(
            all(image.get("width") == "16" for image in titled)
        )
        self.assertTrue(
            all(image.get("height") == "16" for image in titled)
        )
        self.assertTrue(
            any("Aug 21, 2026 · 0 tokens" in image["title"] for image in titled)
        )
        self.assertTrue(all("ccusage" not in image["title"] for image in titled))
        self.assertTrue(all("CPA" not in image["title"] for image in titled))
        self.assertNotIn("Less", html)
        self.assertNotIn("More", html)

    def test_sixty_day_window_uses_dynamic_full_week_boundaries(self):
        cases = (
            (date(2026, 8, 21), 9, 160),
            (date(2026, 3, 1), 10, 178),
        )
        for end, weeks, width in cases:
            with self.subTest(end=end):
                start = end - timedelta(days=DISPLAY_DAYS - 1)
                html = render_heatmap([], start, end)
                parser = ImageParser()
                parser.feed(html)
                titled = [image for image in parser.images if image.get("title")]
                gaps = [
                    image
                    for image in parser.images
                    if image.get("src", "").endswith("gap.svg")
                ]
                blanks = [
                    image
                    for image in parser.images
                    if image.get("src", "").endswith("blank.svg")
                ]
                self.assertEqual(len(titled), DISPLAY_DAYS)
                self.assertEqual(len(gaps), (weeks - 1) * 7)
                self.assertEqual(len(blanks), weeks * 7 - DISPLAY_DAYS)
                self.assertIn(
                    f'width="{width}"', render_month_labels(start, end)
                )

    def test_leap_day_is_included(self):
        end = date(2024, 3, 1)
        start = end - timedelta(days=364)
        html = render_heatmap([usage(date(2024, 2, 29), total=10)], start, end)
        self.assertIn("Feb 29, 2024 · 10 tokens", html)

    def test_marker_replacement_preserves_other_content(self):
        readme = f"before\n{START_MARKER}\nold\n{END_MARKER}\nafter\n"
        result = replace_marked_section(readme, "new")
        self.assertEqual(
            result, f"before\n{START_MARKER}\n\nnew\n\n{END_MARKER}\nafter\n"
        )

    def test_missing_marker_fails_before_writing_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = root / "README.md"
            readme.write_text("no markers\n")
            with self.assertRaisesRegex(ValueError, "marker pair"):
                update_readme(
                    readme,
                    root / "assets",
                    [],
                    date(2025, 8, 22),
                    date(2026, 8, 21),
                )
            self.assertFalse((root / "assets").exists())


if __name__ == "__main__":
    unittest.main()
