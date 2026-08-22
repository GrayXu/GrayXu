#!/usr/bin/env python3
import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo


def default_machine_id() -> str:
    hostname = socket.gethostname().split(".", 1)[0]
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", hostname).strip("-.")
    return normalized[:64] or "unknown-machine"


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


def copy_snapshot(
    remote_host: str, remote_directory: str, payload: Dict[str, Any]
) -> int:
    machine_id = payload["machine_id"]
    if not isinstance(machine_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", machine_id
    ):
        raise RuntimeError("invalid machine_id")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", remote_host):
        raise RuntimeError("invalid remote host")
    remote_path = Path(remote_directory)
    if (
        not re.fullmatch(r"/[A-Za-z0-9._/-]+", remote_directory)
        or not remote_path.is_absolute()
        or ".." in remote_path.parts
    ):
        raise RuntimeError("remote directory must be an absolute normalized path")

    destination = remote_path / f"{machine_id}.json"
    temporary = remote_path / f".{machine_id}.{os.getpid()}.json.tmp"
    with tempfile.TemporaryDirectory() as directory:
        local_path = Path(directory) / f"{machine_id}.json"
        local_path.write_text(
            json.dumps(payload, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["/usr/bin/scp", "-q", str(local_path), f"{remote_host}:{temporary}"],
            check=True,
            timeout=120,
        )

    source = shlex.quote(str(temporary))
    target = shlex.quote(str(destination))
    command = (
        f"chown root:token-heatmap {source} && chmod 640 {source} && "
        f"mv -f {source} {target}"
    )
    subprocess.run(
        ["/usr/bin/ssh", remote_host, command], check=True, timeout=30
    )
    return len(payload["days"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload a ccusage snapshot")
    parser.add_argument(
        "--remote-host", default=os.environ.get("TOKEN_HEATMAP_REMOTE_HOST", "ali")
    )
    parser.add_argument(
        "--remote-directory",
        default=os.environ.get(
            "TOKEN_HEATMAP_REMOTE_DIRECTORY",
            "/var/lib/token-heatmap/inbox",
        ),
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

    copied = copy_snapshot(args.remote_host, args.remote_directory, payload)
    print(f"copied {copied} daily snapshots to {args.remote_host}")


if __name__ == "__main__":
    main()
