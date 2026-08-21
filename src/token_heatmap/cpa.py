import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .models import DailyUsage


REQUIRED_COLUMNS = {
    "timestamp_ms",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "total_tokens",
    "normalized_uncached_input_tokens",
    "normalized_cache_read_tokens",
}


def _epoch_ms(local_date: date, timezone_info: ZoneInfo) -> int:
    value = datetime.combine(local_date, time.min).replace(tzinfo=timezone_info)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


class CPAAdapter:
    def __init__(self, database_path: Path, timezone_name: str) -> None:
        self.database_path = Path(database_path)
        self.timezone = ZoneInfo(timezone_name)

    def _connect(self) -> sqlite3.Connection:
        uri = "file:{}?mode=ro".format(quote(str(self.database_path), safe="/"))
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(usage_events)")
        }
        missing = REQUIRED_COLUMNS - columns
        if missing:
            connection.close()
            raise RuntimeError(
                "CPA usage_events is missing required columns: {}".format(
                    ", ".join(sorted(missing))
                )
            )
        return connection

    def get_daily_usage(
        self, start_date: date, end_date: date
    ) -> List[DailyUsage]:
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")

        totals: Dict[date, Dict[str, int]] = {}
        current = start_date
        while current <= end_date:
            totals[current] = {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "request_count": 0,
            }
            current += timedelta(days=1)

        start_ms = _epoch_ms(start_date, self.timezone)
        end_ms = _epoch_ms(end_date + timedelta(days=1), self.timezone)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT
                    timestamp_ms,
                    input_tokens,
                    output_tokens,
                    reasoning_tokens,
                    cache_read_tokens,
                    total_tokens,
                    normalized_uncached_input_tokens,
                    normalized_cache_read_tokens
                FROM usage_events
                WHERE timestamp_ms >= ? AND timestamp_ms < ?
                ORDER BY timestamp_ms
                """,
                (start_ms, end_ms),
            )
            for row in rows:
                usage_date = datetime.fromtimestamp(
                    row["timestamp_ms"] / 1000, timezone.utc
                ).astimezone(self.timezone).date()
                values = {
                    key: int(row[key] or 0)
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "reasoning_tokens",
                        "cache_read_tokens",
                        "total_tokens",
                    )
                }
                if any(value < 0 for value in values.values()):
                    raise RuntimeError("CPA usage_events contains negative token counts")
                normalized_uncached = row["normalized_uncached_input_tokens"]
                if normalized_uncached is None:
                    uncached_input = max(
                        values["input_tokens"] - values["cache_read_tokens"], 0
                    )
                else:
                    uncached_input = int(normalized_uncached)
                normalized_cached = row["normalized_cache_read_tokens"]
                cached_input = (
                    values["cache_read_tokens"]
                    if normalized_cached is None
                    else int(normalized_cached)
                )
                if uncached_input < 0 or cached_input < 0:
                    raise RuntimeError("CPA normalized token counts must be non-negative")

                day = totals[usage_date]
                day["input_tokens"] += uncached_input
                day["cached_input_tokens"] += cached_input
                day["output_tokens"] += values["output_tokens"]
                day["reasoning_tokens"] += values["reasoning_tokens"]
                day["total_tokens"] += values["total_tokens"]
                day["request_count"] += 1
        finally:
            connection.close()

        return [
            DailyUsage(
                date=usage_date,
                source="cpa",
                machine_id="",
                input_tokens=totals[usage_date]["input_tokens"],
                cached_input_tokens=totals[usage_date]["cached_input_tokens"],
                output_tokens=totals[usage_date]["output_tokens"],
                reasoning_tokens=totals[usage_date]["reasoning_tokens"],
                total_tokens=totals[usage_date]["total_tokens"],
                request_count=totals[usage_date]["request_count"],
                cost_usd=0.0,
            )
            for usage_date in sorted(totals)
        ]
