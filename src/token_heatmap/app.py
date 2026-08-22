import argparse
import configparser
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from .data import (
    import_ccusage_snapshots,
    load_data_file,
    refresh_daily_usage,
    write_data_file,
)
from .heatmap import DISPLAY_DAYS, update_readme


def load_config(path: Path) -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(path):
        raise RuntimeError(f"config not found: {path}")
    if not config.has_section("app"):
        raise RuntimeError("config requires [app]")
    return config


def required(config: configparser.ConfigParser, section: str, key: str) -> str:
    value = config.get(section, key, fallback="").strip()
    if not value:
        raise RuntimeError(f"config requires [{section}] {key}")
    return value


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
    by_date = {}
    for item in raw_days:
        if not isinstance(item, dict) or not isinstance(item.get("date"), str):
            raise RuntimeError("ccusage returned an invalid daily row")
        usage_date = date.fromisoformat(item["date"])
        if not start_date <= usage_date <= end_date:
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
    completed = subprocess.run(
        [
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
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
    )
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise RuntimeError("ccusage JSON root must be an object")
    return result


def copy_snapshot(
    remote_host: str, remote_directory: str, payload: Dict[str, Any]
) -> None:
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
        or ".." in remote_path.parts
    ):
        raise RuntimeError("invalid remote directory")

    destination = remote_path / f"{machine_id}.json"
    temporary = remote_path / f".{machine_id}.{os.getpid()}.json.tmp"
    with tempfile.TemporaryDirectory() as directory:
        local_path = Path(directory) / destination.name
        local_path.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
        subprocess.run(
            ["/usr/bin/scp", "-q", str(local_path), f"{remote_host}:{temporary}"],
            check=True,
            timeout=120,
        )
    source = shlex.quote(str(temporary))
    target = shlex.quote(str(destination))
    subprocess.run(
        [
            "/usr/bin/ssh",
            remote_host,
            f"chown root:token-heatmap {source} && chmod 640 {source} && "
            f"mv -f {source} {target}",
        ],
        check=True,
        timeout=30,
    )


def sync_sender(config: configparser.ConfigParser) -> None:
    timezone_name = config.get("app", "timezone", fallback="Asia/Shanghai")
    timezone = ZoneInfo(timezone_name)
    days = config.getint("sender", "days", fallback=3)
    if not 1 <= days <= 400:
        raise RuntimeError("sender days must be between 1 and 400")
    bunx = config.get("sender", "bunx", fallback="bunx")
    bunx = bunx if "/" in bunx else shutil.which(bunx)
    if not bunx:
        raise RuntimeError("bunx was not found")
    machine_id = config.get("sender", "machine_id", fallback=default_machine_id())
    today = datetime.now(timezone).date()
    start_date = today - timedelta(days=days - 1)
    report = run_ccusage(bunx, timezone_name, start_date, today)
    payload = {
        "machine_id": machine_id,
        "generated_at": datetime.now(timezone).isoformat(),
        "days": build_days(report, start_date, today),
    }
    copy_snapshot(
        required(config, "sender", "ssh_host"),
        required(config, "sender", "remote_inbox"),
        payload,
    )


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def sync_branch(repo: Path, branch: str) -> None:
    if git(repo, "diff", "--quiet", check=False).returncode or git(
        repo, "diff", "--cached", "--quiet", check=False
    ).returncode:
        raise RuntimeError("data checkout is dirty")
    git(repo, "fetch", "origin", branch)
    remote = f"origin/{branch}"
    if git(repo, "merge-base", "--is-ancestor", "HEAD", remote, check=False).returncode == 0:
        git(repo, "merge", "--ff-only", remote)
    elif git(repo, "merge-base", "--is-ancestor", remote, "HEAD", check=False).returncode:
        git(repo, "rebase", remote)


def sync_primary(config: configparser.ConfigParser) -> None:
    timezone_name = config.get("app", "timezone", fallback="Asia/Shanghai")
    timezone = ZoneInfo(timezone_name)
    database = Path(required(config, "primary", "database"))
    inbox = Path(required(config, "primary", "inbox"))
    data_repo = Path(required(config, "primary", "data_repo"))
    data_branch = config.get("primary", "data_branch", fallback="token-data")
    cpa = config.get("primary", "cpa_database", fallback="").strip()
    history_days = config.getint("primary", "history_days", fallback=365)
    if history_days < DISPLAY_DAYS:
        raise RuntimeError(f"primary history_days must be at least {DISPLAY_DAYS}")

    sync_branch(data_repo, data_branch)
    import_ccusage_snapshots(database, inbox, timezone_name)
    today = datetime.now(timezone).date()
    daily_usage = refresh_daily_usage(
        database,
        Path(cpa) if cpa else None,
        timezone_name,
        history_days,
        today,
    )
    write_data_file(data_repo / "daily_usage.json", daily_usage, timezone_name, today)
    git(data_repo, "add", "--", "daily_usage.json")
    if git(data_repo, "diff", "--cached", "--quiet", check=False).returncode:
        author = config.get("primary", "git_author_name", fallback="Token Heatmap Bot")
        email = config.get(
            "primary",
            "git_author_email",
            fallback="token-heatmap@users.noreply.github.com",
        )
        git(
            data_repo,
            "-c",
            f"user.name={author}",
            "-c",
            f"user.email={email}",
            "commit",
            "-m",
            "chore: update token usage data",
            "--",
            "daily_usage.json",
        )
    ahead = git(data_repo, "rev-list", "--count", f"origin/{data_branch}..HEAD")
    if int(ahead.stdout):
        git(data_repo, "push", "origin", f"HEAD:{data_branch}")


def render(input_path: Path, repo: Path) -> None:
    usage_data = load_data_file(input_path)
    update_readme(
        repo / "README.md",
        repo / "assets" / "heatmap",
        usage_data.days,
        usage_data.days[0].date,
        usage_data.days[-1].date,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync and render token heatmaps")
    parser.add_argument("--config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync")
    renderer = subparsers.add_parser("render")
    renderer.add_argument("--input", type=Path, required=True)
    renderer.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "render":
        render(args.input, args.repo)
        return
    config_path = args.config or os.environ.get("TOKEN_HEATMAP_CONFIG")
    if not config_path:
        raise SystemExit("--config or TOKEN_HEATMAP_CONFIG is required")
    config = load_config(Path(config_path).expanduser())
    if config.getboolean("app", "primary", fallback=False):
        sync_primary(config)
    else:
        sync_sender(config)


if __name__ == "__main__":
    main()
