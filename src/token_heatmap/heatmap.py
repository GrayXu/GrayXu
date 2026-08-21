import hashlib
import html
import math
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from .models import AggregatedUsage


CELL_SIZE = 16
GAP_SIZE = 2
DISPLAY_DAYS = 60
STATS_WIDTH = 118
STATS_HEIGHT = 150
USAGE_LEVELS = 10
START_MARKER = "<!-- TOKEN-HEATMAP:START -->"
END_MARKER = "<!-- TOKEN-HEATMAP:END -->"

LEVEL_COLORS = {
    0: None,
    1: "#9be9a8",
    2: "#77d98a",
    3: "#4dc86b",
    4: "#3bb85c",
    5: "#34a953",
    6: "#2d9549",
    7: "#267f40",
    8: "#1f6937",
    9: "#165630",
    10: "#0e4429",
}


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", text=True
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_static_assets(asset_directory: Path) -> None:
    asset_directory = Path(asset_directory)
    for level, color in LEVEL_COLORS.items():
        if level == 0:
            rectangle = (
                '<rect x="0.5" y="0.5" width="15" height="15" rx="3" '
                'fill="none" stroke="#8c959f"/>'
            )
        else:
            rectangle = (
                f'<rect width="16" height="16" rx="3" fill="{color}"/>'
            )
        atomic_write(
            asset_directory / f"level-{level}.svg",
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
            f'viewBox="0 0 16 16">{rectangle}</svg>\n',
        )
    atomic_write(
        asset_directory / "blank.svg",
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
        'viewBox="0 0 16 16"/>\n',
    )
    atomic_write(
        asset_directory / "gap.svg",
        '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="16" '
        'viewBox="0 0 2 16"/>\n',
    )


def _nearest_rank(values: Sequence[int], percentile: float) -> int:
    index = max(0, math.ceil(percentile * len(values)) - 1)
    return values[index]


def percentile_thresholds(values: Iterable[int]) -> Tuple[int, ...]:
    positive = sorted(value for value in values if value > 0)
    if not positive:
        return tuple(0 for _ in range(USAGE_LEVELS - 1))
    return tuple(
        _nearest_rank(positive, level / USAGE_LEVELS)
        for level in range(1, USAGE_LEVELS)
    )


def usage_level(value: int, thresholds: Sequence[int]) -> int:
    if value <= 0:
        return 0
    for level, threshold in enumerate(thresholds, start=1):
        if value <= threshold:
            return level
    return len(thresholds) + 1


def format_tokens(value: int) -> str:
    units = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
    for scale, suffix in units:
        if value >= scale:
            number = value / scale
            precision = 0 if number >= 100 else 1
            rendered = f"{number:.{precision}f}"
            if "." in rendered:
                rendered = rendered.rstrip("0").rstrip(".")
            return f"{rendered}{suffix}"
    return str(value)


def _date_label(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def _day_image(usage: AggregatedUsage, level: int) -> str:
    title = f"{_date_label(usage.date)} · {format_tokens(usage.total_tokens)} tokens"
    return (
        f'<img src="./assets/heatmap/level-{level}.svg" width="{CELL_SIZE}" '
        f'height="{CELL_SIZE}" alt="" title="{html.escape(title, quote=True)}">'
    )


def _blank_image() -> str:
    return (
        f'<img src="./assets/heatmap/blank.svg" width="{CELL_SIZE}" '
        f'height="{CELL_SIZE}" alt="">'
    )


def _gap_image() -> str:
    return (
        f'<img src="./assets/heatmap/gap.svg" width="{GAP_SIZE}" '
        f'height="{CELL_SIZE}" alt="">'
    )


def grid_bounds(start_date: date, end_date: date) -> Tuple[date, date]:
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    sunday_offset = (start_date.weekday() + 1) % 7
    saturday_offset = (5 - end_date.weekday()) % 7
    return (
        start_date - timedelta(days=sunday_offset),
        end_date + timedelta(days=saturday_offset),
    )


def _grid_week_count(start_date: date, end_date: date) -> int:
    grid_start, grid_end = grid_bounds(start_date, end_date)
    return (grid_end - grid_start).days // 7 + 1


def render_month_labels(start_date: date, end_date: date) -> str:
    grid_start, _ = grid_bounds(start_date, end_date)
    weeks = _grid_week_count(start_date, end_date)
    width = weeks * CELL_SIZE + (weeks - 1) * GAP_SIZE
    labels: List[Tuple[int, str]] = [(0, grid_start.strftime("%b"))]
    current = date(start_date.year, start_date.month, 1)
    if current < start_date:
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    while current <= end_date:
        week = (current - grid_start).days // 7
        month = current.strftime("%b")
        if 0 <= week < weeks:
            if week == labels[-1][0]:
                labels[-1] = (week, month)
            elif week - labels[-1][0] >= 2:
                labels.append((week, month))
            elif labels[-1][0] == 0:
                labels[-1] = (week, month)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    text = "".join(
        f'<text x="{week * (CELL_SIZE + GAP_SIZE)}" y="11">{month}</text>'
        for week, month in labels
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="14" '
        f'viewBox="0 0 {width} 14"><g fill="#8c959f" font-size="10" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif">'
        f"{text}</g></svg>\n"
    )


def render_heatmap(
    daily_usage: Sequence[AggregatedUsage],
    start_date: date,
    end_date: date,
    header_html: str = "",
) -> str:
    usage_by_date: Dict[date, AggregatedUsage] = {
        usage.date: usage for usage in daily_usage
    }
    zero_template = {
        "ccusage_tokens": 0,
        "cpa_tokens": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "request_count": 0,
        "cost_usd": 0.0,
    }
    current = start_date
    while current <= end_date:
        if current not in usage_by_date:
            usage_by_date[current] = AggregatedUsage(date=current, **zero_template)
        current += timedelta(days=1)

    thresholds = percentile_thresholds(
        usage_by_date[current_date].total_tokens
        for current_date in sorted(usage_by_date)
        if start_date <= current_date <= end_date
    )
    grid_start, _ = grid_bounds(start_date, end_date)
    weeks = _grid_week_count(start_date, end_date)

    rows = []
    for weekday in range(7):
        cells = []
        for week in range(weeks):
            if week:
                cells.append(_gap_image())
            current_date = grid_start + timedelta(days=week * 7 + weekday)
            if start_date <= current_date <= end_date:
                usage = usage_by_date[current_date]
                cells.append(
                    _day_image(usage, usage_level(usage.total_tokens, thresholds))
                )
            else:
                cells.append(_blank_image())
        rows.append("".join(cells))
    if header_html:
        rows.insert(0, header_html)
    return "<h6><sub>" + "<br>\n".join(rows) + "</sub></h6>"


def _summary_values(
    daily_usage: Sequence[AggregatedUsage], end_date: date
) -> Tuple[int, int, int]:
    by_date = {usage.date: usage for usage in daily_usage}
    today_tokens = by_date.get(end_date).total_tokens if end_date in by_date else 0
    week_start = end_date - timedelta(days=end_date.weekday())
    week_tokens = sum(
        usage.total_tokens
        for usage in daily_usage
        if week_start <= usage.date <= end_date
    )
    month_tokens = sum(
        usage.total_tokens
        for usage in daily_usage
        if usage.date.year == end_date.year and usage.date.month == end_date.month
    )
    return today_tokens, week_tokens, month_tokens


def _summary_text(values: Tuple[int, int, int], separator: str) -> str:
    today_tokens, week_tokens, month_tokens = values
    return separator.join(
        (
            f"{format_tokens(today_tokens)} today",
            f"{format_tokens(week_tokens)} this week",
            f"{format_tokens(month_tokens)} this month",
        )
    )


def _stats_asset_name(values: Tuple[int, int, int]) -> str:
    identity = ":".join(str(value) for value in values).encode("ascii")
    digest = hashlib.sha256(identity).hexdigest()[:12]
    return f"stats-{digest}.svg"


def render_stats_svg(
    daily_usage: Sequence[AggregatedUsage], end_date: date
) -> str:
    today_tokens, week_tokens, month_tokens = _summary_values(daily_usage, end_date)
    lines = (
        (30, f"{format_tokens(today_tokens)} today"),
        (78, f"{format_tokens(week_tokens)} this week"),
        (126, f"{format_tokens(month_tokens)} this month"),
    )
    text = "".join(
        f'<text x="0" y="{y}">{html.escape(label)}</text>' for y, label in lines
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{STATS_WIDTH}" '
        f'height="{STATS_HEIGHT}" viewBox="0 0 {STATS_WIDTH} {STATS_HEIGHT}">'
        '<style>text{fill:#24292f}@media(prefers-color-scheme:dark)'
        '{text{fill:#f0f6fc}}</style>'
        '<g font-size="15" font-weight="600" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif">'
        f"{text}</g></svg>\n"
    )


def render_section(
    daily_usage: Sequence[AggregatedUsage], start_date: date, end_date: date
) -> str:
    summary_values = _summary_values(daily_usage, end_date)
    summary = _summary_text(summary_values, "; ")
    stats_asset = _stats_asset_name(summary_values)
    weeks = _grid_week_count(start_date, end_date)
    width = weeks * CELL_SIZE + (weeks - 1) * GAP_SIZE
    stats = (
        f'<img align="left" src="./assets/heatmap/{stats_asset}" '
        f'width="{STATS_WIDTH}" height="{STATS_HEIGHT}" '
        f'alt="{html.escape(summary, quote=True)}">'
    )
    labels = (
        f'<img src="./assets/heatmap/month-labels.svg" width="{width}" '
        'height="14" alt="Month labels">'
    )
    heatmap = render_heatmap(
        daily_usage, start_date, end_date, header_html=labels
    )
    return f"<div>{stats}{heatmap}</div>"


def replace_marked_section(readme: str, generated: str) -> str:
    if readme.count(START_MARKER) != 1 or readme.count(END_MARKER) != 1:
        raise ValueError("README must contain exactly one token heatmap marker pair")
    start_index = readme.index(START_MARKER) + len(START_MARKER)
    end_index = readme.index(END_MARKER)
    if end_index < start_index:
        raise ValueError("README token heatmap markers are reversed")
    return readme[:start_index] + "\n\n" + generated + "\n\n" + readme[end_index:]


def update_readme(
    readme_path: Path,
    asset_directory: Path,
    daily_usage: Sequence[AggregatedUsage],
    start_date: date,
    end_date: date,
) -> None:
    readme = Path(readme_path).read_text(encoding="utf-8")
    generated = render_section(daily_usage, start_date, end_date)
    updated_readme = replace_marked_section(readme, generated)
    write_static_assets(asset_directory)
    atomic_write(
        Path(asset_directory) / "month-labels.svg",
        render_month_labels(start_date, end_date),
    )
    atomic_write(
        Path(asset_directory) / _stats_asset_name(
            _summary_values(daily_usage, end_date)
        ),
        render_stats_svg(daily_usage, end_date),
    )
    current_stats = _stats_asset_name(_summary_values(daily_usage, end_date))
    for stats_path in Path(asset_directory).glob("stats*.svg"):
        if stats_path.name != current_stats:
            stats_path.unlink()
    atomic_write(Path(readme_path), updated_readme)
