import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from .models import DailyUsage


MACHINE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _non_negative_int(day: Dict[str, Any], key: str) -> int:
    value = day.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _non_negative_float(day: Dict[str, Any], key: str) -> float:
    value = day.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a non-negative number")
    result = float(value)
    if result < 0 or not math.isfinite(result):
        raise ValueError(f"{key} must be a finite non-negative number")
    return result


def parse_ccusage_payload(
    payload: Any, timezone_name: str, max_days: int = 400
) -> List[DailyUsage]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    machine_id = payload.get("machine_id")
    if not isinstance(machine_id, str) or not MACHINE_ID_PATTERN.fullmatch(machine_id):
        raise ValueError("machine_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")

    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("generated_at must be an ISO-8601 string")
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("generated_at must be an ISO-8601 string") from error
    if generated.tzinfo is None:
        raise ValueError("generated_at must include a UTC offset")

    days = payload.get("days")
    if not isinstance(days, list) or not 1 <= len(days) <= max_days:
        raise ValueError(f"days must contain between 1 and {max_days} rows")

    timezone = ZoneInfo(timezone_name)
    today = datetime.now(timezone).date()
    oldest = today - timedelta(days=max_days - 1)
    newest = today + timedelta(days=1)
    seen_dates = set()
    result = []

    for item in days:
        if not isinstance(item, dict):
            raise ValueError("each days row must be a JSON object")
        raw_date = item.get("date")
        if not isinstance(raw_date, str):
            raise ValueError("date must be an ISO calendar date")
        try:
            usage_date = date.fromisoformat(raw_date)
        except ValueError as error:
            raise ValueError("date must be an ISO calendar date") from error
        if usage_date in seen_dates:
            raise ValueError(f"duplicate date: {raw_date}")
        if not oldest <= usage_date <= newest:
            raise ValueError(f"date outside accepted snapshot window: {raw_date}")
        seen_dates.add(usage_date)

        result.append(
            DailyUsage(
                date=usage_date,
                source="ccusage",
                machine_id=machine_id,
                input_tokens=_non_negative_int(item, "input_tokens"),
                cached_input_tokens=_non_negative_int(
                    item, "cached_input_tokens"
                ),
                output_tokens=_non_negative_int(item, "output_tokens"),
                reasoning_tokens=_non_negative_int(item, "reasoning_tokens"),
                total_tokens=_non_negative_int(item, "total_tokens"),
                request_count=_non_negative_int(item, "request_count"),
                cost_usd=_non_negative_float(item, "cost_usd"),
            )
        )

    return result
