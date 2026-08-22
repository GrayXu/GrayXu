import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from .ccusage import parse_ccusage_payload
from .cpa import CPAAdapter
from .db import (
    aggregate_range,
    connect,
    fetch_daily_usage,
    replace_source_range,
    upsert_source_usage,
)
from .heatmap import DISPLAY_DAYS, atomic_write
from .models import AggregatedUsage


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class UsageData:
    timezone: str
    generated_at: datetime
    days: List[AggregatedUsage]


def import_ccusage_snapshots(
    database: Path, inbox: Path, timezone_name: str
) -> int:
    usages = []
    for path in sorted(Path(inbox).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        usages.extend(parse_ccusage_payload(payload, timezone_name))
    if not usages:
        return 0
    connection = connect(database)
    try:
        return upsert_source_usage(connection, usages)
    finally:
        connection.close()


def refresh_daily_usage(
    database: Path,
    cpa_database: Optional[Path],
    timezone_name: str,
    history_days: int,
    today: date,
) -> List[AggregatedUsage]:
    start_date = today - timedelta(days=history_days - 1)
    cpa_usage = (
        CPAAdapter(cpa_database, timezone_name).get_daily_usage(start_date, today)
        if cpa_database
        else []
    )
    connection = connect(database)
    try:
        replace_source_range(connection, "cpa", "", start_date, today, cpa_usage)
        aggregate_range(connection, start_date, today)
        return fetch_daily_usage(connection, start_date, today)
    finally:
        connection.close()


def build_data_payload(
    daily_usage: Sequence[AggregatedUsage],
    timezone_name: str,
    end_date: date,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    timezone = ZoneInfo(timezone_name)
    timestamp = generated_at or datetime.now(timezone)
    if timestamp.tzinfo is None:
        raise ValueError("generated_at must include a UTC offset")

    start_date = end_date - timedelta(days=DISPLAY_DAYS - 1)
    by_date = {usage.date: usage.total_tokens for usage in daily_usage}
    days = []
    current = start_date
    while current <= end_date:
        total_tokens = int(by_date.get(current, 0))
        if total_tokens < 0:
            raise ValueError("total_tokens must be non-negative")
        days.append({"date": current.isoformat(), "total_tokens": total_tokens})
        current += timedelta(days=1)

    return {
        "schema_version": SCHEMA_VERSION,
        "timezone": timezone_name,
        "generated_at": timestamp.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "days": days,
    }


def write_data_file(
    path: Path,
    daily_usage: Sequence[AggregatedUsage],
    timezone_name: str,
    end_date: date,
) -> None:
    payload = build_data_payload(daily_usage, timezone_name, end_date)
    atomic_write(
        Path(path), json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def _required_string(payload: Dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def load_data_file(path: Path) -> UsageData:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("data file must contain valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("data file root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported data schema_version")

    timezone_name = _required_string(payload, "timezone")
    ZoneInfo(timezone_name)
    try:
        generated_at = datetime.fromisoformat(
            _required_string(payload, "generated_at").replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ValueError("generated_at must be ISO-8601") from error
    if generated_at.tzinfo is None:
        raise ValueError("generated_at must include a UTC offset")

    try:
        start_date = date.fromisoformat(_required_string(payload, "start_date"))
        end_date = date.fromisoformat(_required_string(payload, "end_date"))
    except ValueError as error:
        raise ValueError("start_date and end_date must be ISO dates") from error
    if end_date - start_date != timedelta(days=DISPLAY_DAYS - 1):
        raise ValueError(f"data file must cover exactly {DISPLAY_DAYS} days")

    raw_days = payload.get("days")
    if not isinstance(raw_days, list) or len(raw_days) != DISPLAY_DAYS:
        raise ValueError(f"days must contain exactly {DISPLAY_DAYS} rows")
    days = []
    for index, item in enumerate(raw_days):
        if not isinstance(item, dict):
            raise ValueError("each days row must be an object")
        expected_date = start_date + timedelta(days=index)
        try:
            usage_date = date.fromisoformat(_required_string(item, "date"))
        except ValueError as error:
            raise ValueError("days dates must be ISO dates") from error
        if usage_date != expected_date:
            raise ValueError("days must be continuous and ordered")
        total_tokens = item.get("total_tokens")
        if (
            isinstance(total_tokens, bool)
            or not isinstance(total_tokens, int)
            or total_tokens < 0
        ):
            raise ValueError("total_tokens must be a non-negative integer")
        days.append(
            AggregatedUsage(
                date=usage_date,
                ccusage_tokens=0,
                cpa_tokens=0,
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                total_tokens=total_tokens,
                request_count=0,
                cost_usd=0.0,
            )
        )

    if days[-1].date != end_date:
        raise ValueError("end_date does not match the final days row")
    return UsageData(timezone_name, generated_at, days)
