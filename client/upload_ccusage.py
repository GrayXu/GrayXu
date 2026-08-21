#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


def default_machine_id() -> str:
    hostname = socket.gethostname().split(".", 1)[0]
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", hostname).strip("-.")
    return normalized[:64] or "unknown-machine"


def read_token(path: Path) -> str:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError(f"token file permissions must be 600, got {mode:o}")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("ingest token must contain at least 32 characters")
    return token


def _integer(item: Dict[str, Any], key: str) -> int:
    value = item.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"ccusage returned an invalid {key}")
    return value


def build_days(report: Dict[str, Any], start_date: date, end_date: date) -> List[dict]:
    raw_days = report.get("daily")
    if not isinstance(raw_days, list):
        raise RuntimeError("ccusage JSON does not contain a daily array")
    by_date: Dict[str, Dict[str, Any]] = {}
    for item in raw_days:
        if not isinstance(item, dict) or not isinstance(item.get("date"), str):
            raise RuntimeError("ccusage returned an invalid daily row")
        parsed_date = date.fromisoformat(item["date"])
        if not start_date <= parsed_date <= end_date:
            continue
        if item["date"] in by_date:
            raise RuntimeError(f"ccusage returned duplicate date {item['date']}")
        by_date[item["date"]] = item

    days = []
    current = start_date
    while current <= end_date:
        item = by_date.get(current.isoformat(), {})
        cost = item.get("costUSD", 0.0)
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            raise RuntimeError("ccusage returned an invalid costUSD")
        days.append(
            {
                "date": current.isoformat(),
                "input_tokens": _integer(item, "inputTokens"),
                "cached_input_tokens": _integer(item, "cacheReadTokens"),
                "output_tokens": _integer(item, "outputTokens"),
                "reasoning_tokens": _integer(item, "reasoningOutputTokens"),
                "total_tokens": _integer(item, "totalTokens"),
                "request_count": 0,
                "cost_usd": float(cost),
            }
        )
        current += timedelta(days=1)
    return days


def run_ccusage(
    bunx: str, timezone_name: str, start_date: date, end_date: date
) -> Dict[str, Any]:
    command = [
        bunx,
        "ccusage",
        "codex",
        "daily",
        "--speed",
        "fast",
        "--timezone",
        timezone_name,
        "--since",
        start_date.isoformat(),
        "--until",
        end_date.isoformat(),
        "--json",
        "--no-color",
    ]
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("ccusage did not return valid JSON") from error
    if not isinstance(result, dict):
        raise RuntimeError("ccusage JSON root must be an object")
    return result


def upload(endpoint: str, token: str, payload: dict) -> dict:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("endpoint must use HTTPS unless it targets localhost")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "token-heatmap-ccusage/0.1",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        detail = error.read(512).decode("utf-8", errors="replace")
        raise RuntimeError(f"upload failed with HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"upload failed: {error.reason}") from error
    if not isinstance(result, dict):
        raise RuntimeError("server returned an invalid response")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload a ccusage snapshot")
    parser.add_argument(
        "--endpoint", default=os.environ.get("TOKEN_HEATMAP_ENDPOINT")
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(
            os.environ.get(
                "TOKEN_HEATMAP_TOKEN_FILE",
                "~/.config/token-heatmap/ingest-token",
            )
        ).expanduser(),
    )
    parser.add_argument(
        "--machine-id",
        default=os.environ.get("TOKEN_HEATMAP_MACHINE_ID", default_machine_id()),
    )
    parser.add_argument(
        "--timezone",
        default=os.environ.get("TOKEN_HEATMAP_TIMEZONE", "Asia/Shanghai"),
    )
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--bunx", default=os.environ.get("TOKEN_HEATMAP_BUNX"))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not 1 <= args.days <= 400:
        raise SystemExit("--days must be between 1 and 400")
    if not args.endpoint and not args.dry_run:
        raise SystemExit("--endpoint or TOKEN_HEATMAP_ENDPOINT is required")
    bunx = args.bunx or shutil.which("bunx")
    if not bunx:
        raise SystemExit("bunx was not found")

    timezone = ZoneInfo(args.timezone)
    today = datetime.now(timezone).date()
    start_date = today - timedelta(days=args.days - 1)
    report = run_ccusage(bunx, args.timezone, start_date, today)
    payload = {
        "machine_id": args.machine_id,
        "generated_at": datetime.now(timezone).isoformat(),
        "days": build_days(report, start_date, today),
    }
    if args.dry_run:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    token = read_token(args.token_file)
    result = upload(args.endpoint, token, payload)
    print(f"uploaded {result.get('upserted', 0)} daily snapshots")


if __name__ == "__main__":
    main()
