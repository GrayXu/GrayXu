"""Recover exact published daily totals without inventing source breakdowns."""

import argparse
import json
import sqlite3
import subprocess
import tempfile
from contextlib import nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import quote

from .data import load_data_file
from .db import connect, transaction
from .heatmap import atomic_write


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *arguments], text=True)


def history_maxima(
    repo: Path, ref: str = "origin/token-data", since_commit: Optional[str] = None
) -> Tuple[Dict[date, Tuple[int, str]], int]:
    head = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    commits = git(repo, "rev-list", head, "--", "daily_usage.json").splitlines()
    if since_commit:
        first = git(repo, "rev-parse", "--verify", f"{since_commit}^{{commit}}").strip()
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", first, head],
            check=True,
        )
        allowed = set(git(repo, "rev-list", f"{first}..{head}").splitlines())
        allowed.add(first)
        commits = [commit for commit in commits if commit in allowed]
    if not commits:
        raise ValueError("no daily_usage.json history found in the requested range")

    maxima = {}
    with tempfile.TemporaryDirectory(prefix="token-heatmap-history-") as directory:
        snapshot = Path(directory) / "daily_usage.json"
        for commit in commits:
            snapshot.write_text(git(repo, "show", f"{commit}:daily_usage.json"))
            for usage in load_data_file(snapshot).days:
                previous = maxima.get(usage.date)
                if previous is None or usage.total_tokens > previous[0]:
                    maxima[usage.date] = (usage.total_tokens, commit)
    return maxima, len(commits)


def restore_totals(
    database: Path, maxima: Dict[date, Tuple[int, str]], apply: bool = False
) -> dict:
    for usage_date, (total, _) in maxima.items():
        if not isinstance(usage_date, date):
            raise ValueError("recovery dates must be calendar dates")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ValueError("recovery totals must be non-negative integers")

    if apply:
        connection = connect(database)
    else:
        uri = "file:{}?mode=ro".format(quote(str(database.resolve()), safe="/"))
        connection = sqlite3.connect(uri, uri=True)
    try:
        with transaction(connection) if apply else nullcontext():
            current = dict(
                connection.execute("SELECT date, total_tokens FROM daily_usage")
            )
            changes = []
            for usage_date, (total, commit) in sorted(maxima.items()):
                previous = current.get(usage_date.isoformat(), 0)
                if total > previous:
                    changes.append(
                        {
                            "date": usage_date.isoformat(),
                            "previous_tokens": previous,
                            "total_tokens": total,
                            "added_tokens": total - previous,
                            "commit": commit,
                        }
                    )
            if apply:
                updated_at = datetime.now(timezone.utc).isoformat()
                connection.executemany(
                    """
                    INSERT INTO daily_usage (date, total_tokens, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        total_tokens = MAX(daily_usage.total_tokens, excluded.total_tokens),
                        updated_at = excluded.updated_at
                    """,
                    [
                        (item["date"], item["total_tokens"], updated_at)
                        for item in changes
                    ],
                )
    finally:
        connection.close()
    return {
        "applied": apply,
        "changed_dates": len(changes),
        "total_added_tokens": sum(item["added_tokens"] for item in changes),
        "changes": changes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--ref", default="origin/token-data")
    parser.add_argument(
        "--since-commit", help="include this commit and its descendants"
    )
    parser.add_argument(
        "--apply", action="store_true", help="write maxima; default is read-only"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    maxima, count = history_maxima(args.repo, args.ref, args.since_commit)
    report = restore_totals(args.database, maxima, args.apply)
    report.update(
        {
            "snapshots_scanned": count,
            "dates_found": len(maxima),
            "start_date": min(maxima).isoformat(),
            "end_date": max(maxima).isoformat(),
            "since_commit": args.since_commit,
        }
    )
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        atomic_write(args.report, output)
    print(output, end="")


if __name__ == "__main__":
    main()
