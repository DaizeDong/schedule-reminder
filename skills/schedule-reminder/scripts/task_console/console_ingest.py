"""Fill the task-console database. THE ONLY WRITER.

Runs from the hourly health monitor, not from the web request. That split is most of the point:
reading the Windows Operational event log costs 109 seconds end to end and was on the request path,
so every page load paid for it. Here it is paid once an hour by something nobody is waiting on.

WHY INGESTION IS NOT OPTIONAL. The Operational log is a circular buffer. Measured on this machine:
635 events an hour, 64 MB capacity, so roughly FIVE DAYS before the oldest records are overwritten.
Anything not ingested before then is gone and cannot be recovered from anywhere. That turns this
script from an optimisation into the thing that makes long-range history exist at all.

CONCURRENCY. One writer, readers are read-only, WAL. Writes take BEGIN IMMEDIATE so two ingesters
cannot interleave, and busy_timeout gives a reader time to finish rather than failing it.

DEGRADATION. A source that cannot be read is recorded as a failed ingest_run row and the script
exits non-zero. It never writes a partial pass and calls it done, because an ingest that silently
half-ran and one that succeeded produce the same empty-looking chart later.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import console_store
import history
from rcnorm import norm_rc

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schema.sql"
RUNLOG = HERE / "runlog.ps1"
COLLECT = HERE / "collect.ps1"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def powershell() -> str:
    c = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    return c if os.path.exists(c) else "powershell.exe"


def run_ps(script: Path, args=None, timeout=600):
    p = subprocess.run(
        [powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script)] + list(args or []),
        capture_output=True, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace").strip(),
            p.stderr.decode("utf-8", "replace").strip())


def open_rw():
    p, st = console_store.resolve_db(create_parent=True)
    if st:
        print(f"[{st.code}] {st.message}", file=sys.stderr)
        return None, None
    con = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return con, p


def begin(con):
    con.execute("BEGIN IMMEDIATE")


def note_run(con, source, started, ok, added, msg=""):
    con.execute("INSERT INTO ingest_run(started_at,finished_at,ok,source,added,note) VALUES(?,?,?,?,?,?)",
                (started, now(), 1 if ok else 0, source, added, msg[:500]))


# --------------------------------------------------------------------------- health
def ingest_health(con, path: str, full: bool) -> tuple[bool, int, str]:
    started = now()
    p = Path(os.path.expanduser(path))
    if not p.exists():
        note_run(con, "health", started, False, 0, f"log missing: {p}")
        return False, 0, f"健康日志不存在: {p}"

    # Rotation/truncation detector. The monitor only ever appends, so a changed head or a shrunk
    # file means the log was replaced by hand, and a watermark against the old one would skip rows.
    head = p.open("rb").read(512)
    sig = hashlib.sha256(head).hexdigest()[:16]
    size = p.stat().st_size
    prev = con.execute("SELECT * FROM health_ingest WHERE id=1").fetchone()
    rotated = bool(prev) and (prev["head_sig"] != sig or (prev["size_bytes"] or 0) > size)
    if rotated:
        full = True

    h = history.load(str(p), days=100000 if full else 60)
    if not h.get("available"):
        note_run(con, "health", started, False, 0, h.get("reason", "")[:400])
        return False, 0, h.get("reason", "")

    added = 0
    begin(con)
    try:
        if full:
            con.execute("DELETE FROM health_obs")
        # history.load gives per-day class counts; re-parse for the hour, which it drops.
        for task, day, hour, klass, verdict, lastrun in _iter_health_rows(p, h):
            cur = con.execute(
                "INSERT OR IGNORE INTO health_obs(task,day,hour,klass,verdict,last_run) "
                "VALUES(?,?,?,?,?,?)", (task, day, hour, klass, verdict, lastrun))
            added += cur.rowcount
            con.execute("INSERT INTO task_seen(task,first_seen,last_seen) VALUES(?,?,?) "
                        "ON CONFLICT(task) DO UPDATE SET last_seen=excluded.last_seen",
                        (task, f"{day} {hour:02d}:00:00", f"{day} {hour:02d}:00:00"))
        con.execute(
            "INSERT INTO health_ingest(id,source_path,head_sig,size_bytes,last_ts,last_ingest_at) "
            "VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "source_path=excluded.source_path, head_sig=excluded.head_sig, "
            "size_bytes=excluded.size_bytes, last_ts=excluded.last_ts, "
            "last_ingest_at=excluded.last_ingest_at",
            (str(p), sig, size, h.get("days", [None])[-1] if h.get("days") else None, now()))
        note_run(con, "health", started, True, added,
                 "full rebuild (rotation detected)" if rotated else ("full" if full else "incremental"))
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        note_run(con, "health", started, False, 0, str(e)[:400])
        return False, 0, str(e)
    return True, added, ""


def _iter_health_rows(p: Path, h: dict):
    """Re-walk the log keeping the HOUR, which history.load deduplicates on and then discards."""
    seen = set()
    with p.open(encoding="utf-8-sig", errors="replace") as fh:
        for raw in fh:
            m = history.LINE.match(raw.rstrip())
            if not m:
                continue
            y, mo, d, hh, task, verdict = m.groups()
            task, day, hour = task.strip(), f"{y}-{mo}-{d}", int(hh)
            key = (task, day, hour)
            if key in seen:
                continue
            seen.add(key)
            lr = history.LASTRUN.search(verdict)
            yield task, day, hour, history.classify(verdict), verdict[:300], lr.group(1) if lr else None


# --------------------------------------------------------------------------- run log
def ingest_runlog(con, days: int) -> tuple[bool, int, str]:
    started = now()
    rc, out, err = run_ps(RUNLOG, ["-Days", str(days)], timeout=900)
    if rc != 0 or not out:
        note_run(con, "runlog", started, False, 0, (err or out)[:400])
        return False, 0, f"读运行日志失败: {err or out}"
    try:
        raw = json.loads(out)
    except Exception as e:
        note_run(con, "runlog", started, False, 0, str(e)[:400])
        return False, 0, f"运行日志 JSON 解析失败: {e}"
    if not raw.get("enabled"):
        note_run(con, "runlog", started, False, 0, raw.get("reason", "")[:400])
        return False, 0, raw.get("reason") or "运行历史日志是关闭的"

    oldest = raw.get("oldestRecordId")
    prev = con.execute("SELECT * FROM runlog_ingest WHERE id=1").fetchone()
    epoch = prev["log_epoch"] if prev else 1
    # THE CLEAR DETECTOR. EventRecordID restarts at 1 when the Operational log is cleared. Without
    # this, INSERT OR IGNORE on (record_id) would silently drop every new event whose id collided
    # with an old one, and the only symptom would be a chart that quietly got emptier.
    if prev and oldest is not None and prev["oldest_record_id"] is not None and oldest < prev["oldest_record_id"]:
        epoch = epoch + 1

    added = 0
    begin(con)
    try:
        for e in raw.get("events") or []:
            rid = e.get("rid")
            if rid is None:
                continue
            ts = e["t"]
            rc_raw = e.get("rc")
            cur = con.execute(
                "INSERT OR IGNORE INTO run_event(log_epoch,record_id,task,event_id,ts,day,hour,rc_raw,rc_norm) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (epoch, int(rid), e["task"], int(e["id"]), ts, ts[:10], int(ts[11:13]),
                 rc_raw, norm_rc(rc_raw)))
            added += cur.rowcount
        con.execute(
            "INSERT INTO runlog_ingest(id,log_epoch,max_record_id,oldest_record_id,record_count,last_ingest_at) "
            "VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "log_epoch=excluded.log_epoch, max_record_id=excluded.max_record_id, "
            "oldest_record_id=excluded.oldest_record_id, record_count=excluded.record_count, "
            "last_ingest_at=excluded.last_ingest_at",
            (epoch, raw.get("maxRecordId") or 0, oldest, raw.get("recordCount"), now()))
        note_run(con, "runlog", started, True, added,
                 f"epoch={epoch} oldest={oldest} events={len(raw.get('events') or [])}")
        con.execute("COMMIT")
    except Exception as ex:
        con.execute("ROLLBACK")
        note_run(con, "runlog", started, False, 0, str(ex)[:400])
        return False, 0, str(ex)
    return True, added, ""


# --------------------------------------------------------------------------- durable export
def export_run_events(con) -> tuple[bool, int, str]:
    """Append new run events to a JSONL beside the database.

    THIS FILE IS THE ONLY PLACE SOME OF THIS DATA WILL EXIST. The rest of the database is a cache:
    delete it, re-run --backfill, get it back. That is true of health observations because their
    source log is append-only and never rotates. It is NOT true of run events past about five days,
    because the Windows Operational log is a circular buffer that overwrites them, measured at 635
    events an hour against a 64 MB cap.

    So the durable copy has to live somewhere a version control system can actually hold. Not the
    .sqlite3: it is 4.4 MB and rewritten wholesale every hour, so tracking it would add ~105 MB of
    unreadable binary objects a day. A JSONL is append-only, diffs line by line, and git stores the
    increment.

    Idempotent: the watermark is the (log_epoch, record_id) of the last exported row, kept in meta.
    """
    started = now()
    p, st = console_store.resolve_db()
    if st:
        return False, 0, st.message
    out = Path(p).parent / "run-events.jsonl"

    row = con.execute("SELECT value FROM meta WHERE key='export_watermark'").fetchone()
    mark = row["value"] if row else "0:0"
    try:
        m_epoch, m_rid = (int(x) for x in mark.split(":"))
    except Exception:
        m_epoch, m_rid = 0, 0

    rows = con.execute(
        "SELECT log_epoch,record_id,task,event_id,ts,rc_raw,rc_norm FROM run_event "
        "WHERE log_epoch > ? OR (log_epoch = ? AND record_id > ?) "
        "ORDER BY log_epoch, record_id", (m_epoch, m_epoch, m_rid)).fetchall()
    if not rows:
        note_run(con, "export", started, True, 0, "nothing new")
        return True, 0, ""

    try:
        with out.open("a", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps({
                    "e": r["log_epoch"], "r": r["record_id"], "t": r["task"],
                    "i": r["event_id"], "ts": r["ts"],
                    "rc": r["rc_norm"],           # normalised; NULL when the event carries none
                }, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as e:
        note_run(con, "export", started, False, 0, str(e)[:400])
        return False, 0, str(e)

    last = rows[-1]
    begin(con)
    try:
        con.execute("INSERT INTO meta(key,value) VALUES('export_watermark',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"{last['log_epoch']}:{last['record_id']}",))
        note_run(con, "export", started, True, len(rows), str(out))
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        return False, 0, str(e)
    return True, len(rows), ""


# --------------------------------------------------------------------------- task settings
def ingest_tasks(con) -> tuple[bool, int, str]:
    started = now()
    rc, out, err = run_ps(COLLECT, timeout=300)
    if rc != 0 or not out:
        note_run(con, "tasks", started, False, 0, (err or out)[:400])
        return False, 0, f"采集任务失败: {err or out}"
    try:
        raw = json.loads(out)
    except Exception as e:
        note_run(con, "tasks", started, False, 0, str(e)[:400])
        return False, 0, str(e)

    added = 0
    begin(con)
    try:
        for t in raw.get("tasks", []):
            settings = {k: t.get(k) for k in ("catchup", "retries", "timeout", "multi",
                                              "refuseOnBattery", "stopOnBattery", "runLevel", "userId")}
            trg = json.dumps(t.get("triggersRaw") or [], ensure_ascii=False, sort_keys=True)
            blob = json.dumps({"s": settings, "t": trg, "st": t.get("state")},
                              ensure_ascii=False, sort_keys=True)
            h = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
            cur = con.execute(
                "INSERT INTO task_meta_version(task,config_hash,first_seen,last_seen,state,triggers,settings) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(task,config_hash) DO UPDATE SET last_seen=excluded.last_seen",
                (t["name"], h, now(), now(), t.get("state"), trg,
                 json.dumps(settings, ensure_ascii=False, sort_keys=True)))
            added += 1 if cur.rowcount and cur.lastrowid else 0
            con.execute("INSERT INTO task_seen(task,first_seen,last_seen) VALUES(?,?,?) "
                        "ON CONFLICT(task) DO UPDATE SET last_seen=excluded.last_seen",
                        (t["name"], now(), now()))
        note_run(con, "tasks", started, True, added, f"{len(raw.get('tasks', []))} tasks")
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        note_run(con, "tasks", started, False, 0, str(e)[:400])
        return False, 0, str(e)
    return True, added, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="ingest into the task-console database")
    ap.add_argument("--backfill", action="store_true",
                    help="rebuild health observations from the whole log rather than the recent window")
    ap.add_argument("--health-log", default=os.environ.get("TASK_CONSOLE_HISTORY"))
    ap.add_argument("--run-days", type=int, default=60)
    ap.add_argument("--skip-runlog", action="store_true", help="the slow one; skip for a quick pass")
    a = ap.parse_args()

    con, path = open_rw()
    if not con:
        return 2
    print(f"db: {path}")

    ok_all, msgs = True, []
    if a.health_log:
        ok, n, msg = ingest_health(con, a.health_log, a.backfill)
        print(f"  health : {'ok' if ok else 'FAIL'}  +{n}  {msg}")
        ok_all &= ok
        if msg: msgs.append(msg)
    else:
        print("  health : skipped (no --health-log / TASK_CONSOLE_HISTORY). 这是跳过,不是通过。")
        ok_all = False

    if not a.skip_runlog:
        ok, n, msg = ingest_runlog(con, a.run_days)
        print(f"  runlog : {'ok' if ok else 'FAIL'}  +{n}  {msg}")
        ok_all &= ok
        if msg: msgs.append(msg)

    ok, n, msg = ingest_tasks(con)
    print(f"  tasks  : {'ok' if ok else 'FAIL'}  +{n}  {msg}")
    ok_all &= ok

    ok, n, msg = export_run_events(con)
    print(f"  export : {'ok' if ok else 'FAIL'}  +{n}  {msg}")
    ok_all &= ok

    cov = console_store.coverage(con)
    print(f"  覆盖   : 健康 {cov['health']['rows']} 行 {cov['health']['from']}..{cov['health']['to']}"
          f" · 运行 {cov['runs']['rows']} 行 {cov['runs']['from']}..{cov['runs']['to']}")
    con.close()
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
