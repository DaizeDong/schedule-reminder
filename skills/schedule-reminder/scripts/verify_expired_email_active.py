#!/usr/bin/env python3
"""Integration check for expired email reply filtering in active reminder lists."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import argparse
import tempfile
import private_data
from pathlib import Path


HERE = Path(__file__).resolve().parent
REMINDER = HERE / "reminder.py"
NOW = "2031-04-12T14:00:00Z"


def run(db: str, *args: str) -> dict:
    env = dict(os.environ, SCHEDULE_NOW=NOW, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(REMINDER), "--db", db, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "command failed (%d): %s\n%s" % (proc.returncode, " ".join(args), proc.stderr)
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def add(db: str, title: str, due_at: str | None, source: str, key: str) -> None:
    args = [
        "add",
        "--title",
        title,
        "--source",
        source,
        "--idempotency-key",
        key,
    ]
    if due_at:
        args += ["--due-at", due_at]
    run(db, *args)


def paged_active(db: str, *extra: str) -> list[dict]:
    items: list[dict] = []
    cursor = None
    while True:
        args = ["list", "--active", "--limit", "2", *extra]
        if cursor:
            args += ["--cursor", cursor]
        page = run(db, *args)
        items.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            return items



def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", help="PRIVATE companion directory for synthetic verification output")
    args = parser.parse_args(argv)
    destination = Path(args.data_dir).expanduser() if args.data_dir else private_data.data_dir() / "verification"
    private_data.prove_private(destination)
    os.makedirs(destination, exist_ok=True)
    sys.path.insert(0, str(HERE.parents[2] / "tools"))
    from make_fixtures import expired_email_cases
    case = expired_email_cases()
    with tempfile.TemporaryDirectory(prefix="expired-email-synthetic-", dir=destination) as directory:
        db = str(Path(directory) / "verification.sqlite3")
        run(db, "init")
        for title, due_at, source, key in case["rows"]:
            add(db, title, due_at, source, key)
        assert {item["title"] for item in paged_active(db)} == set(case["active"])
        assert {item["title"] for item in paged_active(db, "--source", "email-monitor")} == set(case["email_active"])
        assert len(run(db, "list", "--source", "email-monitor", "--limit", "20")["items"]) == 5
        due_titles = {item["title"] for item in run(db, "due", "--now", case["now"])["items"]}
        assert set(case["overdue"]) <= due_titles
    print("expired email reply active-list filtering: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
