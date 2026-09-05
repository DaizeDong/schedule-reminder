#!/usr/bin/env python3
"""Integration check for expired email reply filtering in active reminder lists."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


HERE = Path(__file__).resolve().parent
REMINDER = HERE / "reminder.py"
NOW = "2026-08-29T12:00:00Z"


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


def main() -> int:
    # Keep the subprocess-visible fixture directly inside this skill's writable workspace.  Some
    # managed Windows sandboxes give the parent access to a new directory but deny it to children.
    db_path = HERE / (".verify-expired-email-%s.sqlite3" % uuid.uuid4().hex)
    db = str(db_path)
    try:
        run(db, "init")

        add(db, "需回复:已过期", "2026-08-29T11:59:59Z", "email-monitor", "verify:expired")
        add(db, "需回复:正好到期", NOW, "email-monitor", "verify:boundary")
        add(db, "需回复:尚未到期", "2026-08-29T12:00:01Z", "email-monitor", "verify:future")
        add(db, "待查看:虽过期仍保留", "2026-08-29T11:00:00Z", "email-monitor", "verify:review")
        add(db, "需回复:其他来源", "2026-08-29T11:00:00Z", "other-source", "verify:other")
        add(db, "需回复:没有期限", None, "email-monitor", "verify:undated")

        all_active = {item["title"] for item in paged_active(db)}
        assert all_active == {
            "需回复:尚未到期",
            "待查看:虽过期仍保留",
            "需回复:其他来源",
            "需回复:没有期限",
        }, all_active

        email_active = {
            item["title"] for item in paged_active(db, "--source", "email-monitor")
        }
        assert email_active == {
            "需回复:尚未到期",
            "待查看:虽过期仍保留",
            "需回复:没有期限",
        }, email_active

        # Filtering is view-only: retain expired rows for history and the due/tick workflow.
        email_history = run(
            db, "list", "--source", "email-monitor", "--limit", "20"
        )["items"]
        assert len(email_history) == 5
        due_titles = {item["title"] for item in run(db, "due", "--now", NOW)["items"]}
        assert "需回复:已过期" in due_titles
        assert "需回复:正好到期" in due_titles
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(db + suffix).unlink()
            except FileNotFoundError:
                pass

    print("expired email reply active-list filtering: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
