import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List

from .models import AggregatedUsage, DailyUsage


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_usage (
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    machine_id TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    cost_usd REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, source, machine_id)
);

CREATE TABLE IF NOT EXISTS daily_usage (
    date TEXT PRIMARY KEY,
    ccusage_tokens INTEGER NOT NULL DEFAULT 0 CHECK (ccusage_tokens >= 0),
    cpa_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cpa_tokens >= 0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    cost_usd REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    updated_at TEXT NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(SCHEMA)
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _usage_values(usage: DailyUsage, updated_at: str) -> tuple:
    return (
        usage.date.isoformat(),
        usage.source,
        usage.machine_id,
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
        usage.total_tokens,
        usage.request_count,
        usage.cost_usd,
        updated_at,
    )


UPSERT_SOURCE_USAGE = """
INSERT INTO source_usage (
    date, source, machine_id, input_tokens, cached_input_tokens,
    output_tokens, reasoning_tokens, total_tokens, request_count,
    cost_usd, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(date, source, machine_id) DO UPDATE SET
    input_tokens = excluded.input_tokens,
    cached_input_tokens = excluded.cached_input_tokens,
    output_tokens = excluded.output_tokens,
    reasoning_tokens = excluded.reasoning_tokens,
    total_tokens = excluded.total_tokens,
    request_count = excluded.request_count,
    cost_usd = excluded.cost_usd,
    updated_at = excluded.updated_at
"""


def upsert_source_usage(
    connection: sqlite3.Connection, usages: Iterable[DailyUsage]
) -> int:
    usage_list = list(usages)
    updated_at = datetime.now(timezone.utc).isoformat()
    with transaction(connection):
        connection.executemany(
            UPSERT_SOURCE_USAGE,
            [_usage_values(usage, updated_at) for usage in usage_list],
        )
    return len(usage_list)


def replace_source_range(
    connection: sqlite3.Connection,
    source: str,
    machine_id: str,
    start_date: date,
    end_date: date,
    usages: Iterable[DailyUsage],
) -> int:
    usage_list = list(usages)
    for usage in usage_list:
        if usage.source != source or usage.machine_id != machine_id:
            raise ValueError("replacement rows must match source and machine_id")
        if not start_date <= usage.date <= end_date:
            raise ValueError("replacement row is outside the requested date range")

    updated_at = datetime.now(timezone.utc).isoformat()
    with transaction(connection):
        connection.execute(
            """
            DELETE FROM source_usage
            WHERE source = ? AND machine_id = ? AND date BETWEEN ? AND ?
            """,
            (source, machine_id, start_date.isoformat(), end_date.isoformat()),
        )
        connection.executemany(
            UPSERT_SOURCE_USAGE,
            [_usage_values(usage, updated_at) for usage in usage_list],
        )
    return len(usage_list)


def aggregate_range(
    connection: sqlite3.Connection, start_date: date, end_date: date
) -> None:
    updated_at = datetime.now(timezone.utc).isoformat()
    start = start_date.isoformat()
    end = end_date.isoformat()
    with transaction(connection):
        connection.execute(
            "DELETE FROM daily_usage WHERE date BETWEEN ? AND ?", (start, end)
        )
        connection.execute(
            """
            INSERT INTO daily_usage (
                date, ccusage_tokens, cpa_tokens, input_tokens,
                cached_input_tokens, output_tokens, reasoning_tokens,
                total_tokens, request_count, cost_usd, updated_at
            )
            SELECT
                date,
                SUM(CASE WHEN source = 'ccusage' THEN total_tokens ELSE 0 END),
                SUM(CASE WHEN source = 'cpa' THEN total_tokens ELSE 0 END),
                SUM(input_tokens),
                SUM(cached_input_tokens),
                SUM(output_tokens),
                SUM(reasoning_tokens),
                SUM(total_tokens),
                SUM(request_count),
                SUM(cost_usd),
                ?
            FROM source_usage
            WHERE date BETWEEN ? AND ?
            GROUP BY date
            """,
            (updated_at, start, end),
        )


def fetch_daily_usage(
    connection: sqlite3.Connection, start_date: date, end_date: date
) -> List[AggregatedUsage]:
    rows = connection.execute(
        """
        SELECT * FROM daily_usage
        WHERE date BETWEEN ? AND ?
        ORDER BY date
        """,
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    return [
        AggregatedUsage(
            date=date.fromisoformat(row["date"]),
            ccusage_tokens=row["ccusage_tokens"],
            cpa_tokens=row["cpa_tokens"],
            input_tokens=row["input_tokens"],
            cached_input_tokens=row["cached_input_tokens"],
            output_tokens=row["output_tokens"],
            reasoning_tokens=row["reasoning_tokens"],
            total_tokens=row["total_tokens"],
            request_count=row["request_count"],
            cost_usd=row["cost_usd"],
        )
        for row in rows
    ]
