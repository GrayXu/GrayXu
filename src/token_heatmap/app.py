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
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set
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
        cost = item.get("costUSD", item.get("totalCost", 0.0))
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            raise RuntimeError("ccusage returned an invalid cost")
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


def merge_days(reports: List[Dict[str, Any]], start_date: date, end_date: date) -> List[dict]:
    merged = build_days({"daily": []}, start_date, end_date)
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "request_count",
        "cost_usd",
    )
    for report in reports:
        for target, source in zip(merged, build_days(report, start_date, end_date)):
            for field in fields:
                target[field] += source[field]
    return merged


def codex_session_provider(path: Path) -> str:
    try:
        with path.open() as stream:
            entry = json.loads(stream.readline())
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(entry, dict) or entry.get("type") != "session_meta":
        return ""
    if not isinstance(entry.get("payload"), dict):
        return ""
    provider = entry["payload"].get("model_provider")
    return provider if isinstance(provider, str) else ""


@contextmanager
def filtered_codex_home(excluded_providers: Set[str]) -> Iterator[str]:
    source_homes = [
        Path(value.strip())
        for value in os.environ.get("CODEX_HOME", str(Path.home() / ".codex")).split(",")
        if value.strip()
    ]
    with tempfile.TemporaryDirectory(prefix="token-heatmap-codex-") as directory:
        filtered_homes = []
        for index, source_home in enumerate(source_homes):
            target_home = Path(directory) / str(index)
            filtered_homes.append(str(target_home))
            roots = [source_home / name for name in ("sessions", "archived_sessions")]
            roots = [root for root in roots if root.is_dir()] or [source_home]
            for root in roots:
                target_root = target_home / root.relative_to(source_home)
                for source in root.rglob("*.jsonl"):
                    if codex_session_provider(source) in excluded_providers:
                        continue
                    target = target_root / source.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.link(source, target)
        yield ",".join(filtered_homes)


def run_ccusage(
    agent: str,
    ccusage_command: List[str],
    timezone_name: str,
    start_date: date,
    end_date: date,
    codex_home: str = "",
) -> Dict[str, Any]:
    command = [*ccusage_command, agent, "daily"]
    if agent == "codex":
        command.extend(["--speed", "fast"])
    command.extend(
        [
            "--timezone",
            timezone_name,
            "--since",
            start_date.isoformat(),
            "--until",
            end_date.isoformat(),
            "--json",
            "--no-color",
        ]
    )
    environment = os.environ.copy()
    if codex_home:
        environment["CODEX_HOME"] = codex_home
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
        env=environment,
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
    raw_command = config.get("sender", "ccusage_command", fallback="").strip()
    ccusage_command = shlex.split(raw_command) if raw_command else [
        config.get("sender", "bunx", fallback="bunx"),
        "ccusage",
    ]
    executable = ccusage_command[0]
    executable = executable if "/" in executable else shutil.which(executable)
    if not executable:
        raise RuntimeError("ccusage command was not found")
    ccusage_command[0] = executable
    machine_id = config.get("sender", "machine_id", fallback=default_machine_id())
    agents = [
        agent.strip()
        for agent in config.get(
            "sender", "agents", fallback="codex,qodercli,opencode,pi"
        ).split(",")
        if agent.strip()
    ]
    if not agents:
        raise RuntimeError("sender agents must not be empty")
    excluded_codex_providers = {
        provider.strip()
        for provider in config.get(
            "sender", "exclude_codex_providers", fallback=""
        ).split(",")
        if provider.strip()
    }
    today = datetime.now(timezone).date()
    start_date = today - timedelta(days=days - 1)
    codex_filter = (
        filtered_codex_home(excluded_codex_providers)
        if "codex" in agents and excluded_codex_providers
        else nullcontext("")
    )
    with codex_filter as codex_home:
        reports = [
            run_ccusage(
                agent,
                ccusage_command,
                timezone_name,
                start_date,
                today,
                codex_home if agent == "codex" else "",
            )
            for agent in agents
        ]
    payload = {
        "machine_id": machine_id,
        "generated_at": datetime.now(timezone).isoformat(),
        "days": merge_days(reports, start_date, today),
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
