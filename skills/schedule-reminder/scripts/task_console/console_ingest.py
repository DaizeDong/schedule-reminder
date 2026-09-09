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

# 在 pythonw(GUI 子系统)下,每个控制台子程序都要新分配一个控制台。那次分配很慢,
# 而且并发时根本不成立:实测同一条 git 命令,普通 python 下几毫秒,pythonw 下单次
# 4.5 秒,四个并发全部 15 秒超时。加上这个标志之后单次降到 0.08 秒。
#
# 这个坑只在生产形态下出现,而开发期测试都是用普通 python 跑的:探针必须复现真实的
# 调用形状,否则测的是另一个程序。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
        capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
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


def rollback(con):
    """回滚,并且允许「本来就没有事务」。

    ⚠ `begin()` 现在挪进了各处的 try 里。它是 BEGIN IMMEDIATE,在另一个写者持锁、
    busy_timeout 用尽时抛 OperationalError,而它原来在 try **之外** —— 异常直接冒出去,
    没有 note_run,后面几个源也不再摄入,而模块开头写着「读不到的源会被记成一条失败的
    ingest_run 行并非零退出」。
    但光挪进去还不够:except 里那句裸的 `con.execute("ROLLBACK")` 会**再抛一次**
    (cannot rollback - no transaction is active),于是那条承诺照样不成立,
    只是异常换了个名字。**一个把失败处理本身也炸掉的失败处理,等于没有失败处理。**
    """
    try:
        con.execute("ROLLBACK")
    except Exception:
        pass


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
    try:
        begin(con)
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
        rollback(con)
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
def _read_runlog(days: int) -> tuple[dict, str]:
    """读运行日志。先试 EvtQuery 那条快路,不成再回落到 PowerShell。返回 (原始结构, 用了哪条路)。

    ⚠ 这里之前只有 PowerShell 一条路,而它在这台机器上**读不完**:实测
    runlog.ps1 -Days 60 会撞上 900 秒超时,于是 runlog 的摄入从 2026-09-05 起一直失败。
    那个 Windows 通道是滚动缓冲、约五天覆盖一次,所以**没被摄入的历史是永久丢失的**。

    快路(evtlog.py 的 EvtQuery)本来就写好了、也在生产里跑着,只是当时只接进了
    server.py 的读路径,没有接进摄入器。同一份日志、同样 60 天:快路 4.6 秒读到 52991 条,
    慢路 900 秒读不完 —— 差约两百倍。**一个已经写好、量过、还在跑的快路,
    只是没有人把它接到第二个调用点。**

    快路说 enabled=False 是一个**结论**(通道关着 / 读不到通道配置),不是「快路不可用」,
    直接把它交出去:回落到 PowerShell 只会得到同一个结论,还要多花十五分钟。
    """
    try:
        import evtlog
        ok, _why = evtlog.available()
        if ok:
            return evtlog.read(days=days, max_events=500000), "evtlog"
    except Exception:
        pass
    rc, out, err = run_ps(RUNLOG, ["-Days", str(days)], timeout=900)
    if rc != 0 or not out:
        return {"enabled": False, "reason": f"读运行日志失败: {err or out}"}, "powershell"
    try:
        return json.loads(out), "powershell"
    except Exception as e:
        return {"enabled": False, "reason": f"运行日志 JSON 解析失败: {e}"}, "powershell"


def ingest_runlog(con, days: int) -> tuple[bool, int, str]:
    started = now()
    raw, via = _read_runlog(days)
    if not isinstance(raw, dict):
        note_run(con, "runlog", started, False, 0, "读运行日志返回了非预期的结构")
        return False, 0, "读运行日志返回了非预期的结构"
    if not raw.get("enabled"):
        # reason 这个键在两条通路上都可能存在但为 None,所以 .get("reason", "") 会取回 None
        # 而不是空串,再切片就是 TypeError —— 一个「日志关着」的正常分支会炸成异常。
        why = raw.get("reason") or "运行历史日志是关闭的"
        note_run(con, "runlog", started, False, 0, f"[{via}] {why}"[:400])
        return False, 0, why

    # ⚠ enabled=True **不等于**这次读成功了。两条通路都会在「读到一半失败」时
    # 交出 enabled=True + 一个 reason(+ evtlog 那边还有 partial=True 和一批被截断的事件):
    #   - runlog.ps1 在 Get-WinEvent 非「没有事件」的异常里输出 enabled=$true /
    #     reason='read failed: ...' / events=@() 然后 exit 0;
    #   - evtlog.read 在 EvtNext 中途失败时返回 enabled=True / partial=True / 半批事件。
    # 原来这里只判 enabled,于是这两种情况都会走完下面整段:added=0、
    # ingest_run 落一行 ok=1、**runlog-ingest-ok.txt 的 mtime 被刷新**,
    # 而那个戳文件的 docstring 承诺的是「只有成功的 runlog 摄入才写」。
    # 于是健康面板对摄入判绿、顶栏写「最后摄入 刚才」,而这个通道约五天覆盖一次 ——
    # 这段没被摄入的运行历史是永久丢失的。
    # 同一份载荷 server.py 那边判的是 available=False:两个读者对同一个事实给出相反答案。
    #
    # 部分失败不当作整轮失败(半批事件仍然值得入库),但**它不许写成功戳**,
    # 也不许在 note 里装作正常。
    read_failed = bool(raw.get("reason")) and not (raw.get("events") or [])
    if read_failed:
        why = raw.get("reason")
        note_run(con, "runlog", started, False, 0, f"[{via}] 读取失败: {why}"[:400])
        return False, 0, why
    partial = bool(raw.get("partial")) or bool(raw.get("reason"))

    oldest = raw.get("oldestRecordId")
    prev = con.execute("SELECT * FROM runlog_ingest WHERE id=1").fetchone()
    epoch = prev["log_epoch"] if prev else 1
    # THE CLEAR DETECTOR. EventRecordID restarts at 1 when the Operational log is cleared. Without
    # this, INSERT OR IGNORE on (record_id) would silently drop every new event whose id collided
    # with an old one, and the only symptom would be a chart that quietly got emptier.
    if prev and oldest is not None and prev["oldest_record_id"] is not None and oldest < prev["oldest_record_id"]:
        epoch = epoch + 1

    added = 0
    try:
        begin(con)
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
        # 走了哪条路要写进去。两条路的读取上限、耗时、能不能读满 60 天都不一样,
        # 事后看一行「摄入成功 0 条」时,不知道它是哪条路读的就没法判断该查哪边。
        note_run(con, "runlog", started, not partial, added,
                 f"[{via}]{' ⚠部分读取: ' + str(raw.get('reason')) if partial else ''}"
                 f" epoch={epoch} oldest={oldest} "
                 f"events={len(raw.get('events') or [])}"
                 + ("" if oldest is not None else " ⚠oldestRecordId 读不到,日志清空探测器这一轮是关的"))
        con.execute("COMMIT")
    except Exception as ex:
        rollback(con)
        note_run(con, "runlog", started, False, 0, str(ex)[:400])
        return False, 0, str(ex)
    # 部分读取不写成功戳。那个文件的全部意义就是「只有成功才写」,
    # 一个在半成功时也刷新的戳,和一个每次运行都刷新的日志文件是同一种东西:
    # 监控盯着它等于什么都没盯。
    if not partial:
        _stamp_runlog_success(added)
    return (not partial), added, (str(raw.get("reason")) if partial else "")


def _stamp_runlog_success(added: int) -> None:
    """Touch a file that ONLY a successful runlog ingest writes.

    The task-creation spec requires a declared artifact that a failing run cannot produce, because
    a monitor watching an artifact that the failure path also writes is watching nothing. Everything
    else this script touches fails that test: the .sqlite3 is written on every run including the
    ones that record a failure, and stdout is not a file.

    last_ingest_at inside runlog_ingest is already success-only -- the failure path ROLLBACKs -- but
    it lives in the database, and the health monitor reads file mtimes, not SQL. So the same fact is
    mirrored where the monitor can see it.

    Written AFTER the COMMIT on purpose. A stamp written before the transaction lands would claim a
    success that a rollback could still take away.
    """
    try:
        p, _ = console_store.resolve_db()
        if not p:
            return
        stamp = Path(p).parent / "runlog-ingest-ok.txt"
        stamp.write_text("%s added=%d" % (now(), added) + chr(10), encoding="utf-8")
    except Exception:
        # A stamp that cannot be written must not fail an ingest that DID succeed. The monitor will
        # see a stale artifact and say so, which is the correct outcome: the work happened, the
        # evidence did not.
        pass


def _watermark_from_file(out: Path) -> tuple[int, int]:
    """从 JSONL 末尾读回最后一条的 (log_epoch, record_id)。读不出来就是 (0, 0)。

    只读文件尾部,不整读:这个文件按设计会长到几十 MB,而每轮摄入都要问它一次。
    (0, 0) 表示「问不出来」,那会让这一轮从 meta 的水位线开始 ——
    对一个空文件是正确的,对一个读不动的文件是保守的(宁可重导也不漏导:
    重复行可以事后按 (epoch, record_id) 去重,丢掉的事件找不回来)。
    """
    try:
        size = out.stat().st_size
    except OSError:
        return 0, 0
    if not size:
        return 0, 0
    try:
        with out.open("rb") as fh:
            # 一行 JSON 约 120 字节,8KB 足够兜住最后一行;文件更短就整读。
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return 0, 0
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            return int(d["e"]), int(d["r"])   # 键名是缩写的,见 export 那段的 json.dumps
        except Exception:
            # 最后一行可能是上一次写到一半的残行。往前再找一条,
            # 而不是把整个文件判成读不出来。
            continue
    return 0, 0


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

    幂等靠的是水位线 = 最后一条已导出记录的 (log_epoch, record_id)。

    ⚠ 水位线的**权威是这个文件本身,不是 meta 表**。这一点必须这样定,因为
    「追加文件」和「提交水位线」不可能原子完成:
      - 先写文件后提交:提交失败(锁超时、崩溃)时文件里已经有那批行,而 meta 还停在旧值,
        下一轮把同一批**再导一遍** —— 而这是运行事件唯一的持久副本,
        按它重建历史的人会得到翻倍的启动次数,文件里也没有任何去重键校验。
      - 先提交后写文件:崩在中间就是 meta 说导过了而文件里没有,那批事件**永久丢失**。
    两种顺序各自会坏,所以不靠顺序:每次从文件末尾读回真实水位线,
    与 meta 里那份取较大者。meta 从此只是一个缓存,坏了不影响正确性。
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

    # 文件说了算。上一轮如果写进去了但没来得及提交水位线,meta 会落后,
    # 而这里读回来的那一条会把它顶上去,于是同一批不会被导第二遍。
    f_epoch, f_rid = _watermark_from_file(out)
    if (f_epoch, f_rid) > (m_epoch, m_rid):
        m_epoch, m_rid = f_epoch, f_rid

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
    try:
        begin(con)
        con.execute("INSERT INTO meta(key,value) VALUES('export_watermark',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"{last['log_epoch']}:{last['record_id']}",))
        note_run(con, "export", started, True, len(rows), str(out))
        con.execute("COMMIT")
    except Exception as e:
        rollback(con)
        # ⚠ 这条失败路径以前**根本不记账**,而 health / runlog / tasks 三条都记。
        # 模块开头写着「读不到的源会被记成一条失败的 ingest_run 行并非零退出」,
        # 而导出这一条不在其中:导出失败之后,账本里既没有成功也没有失败,
        # 那一轮在事后看来就像**从来没跑过导出**。
        # note_run 自己也可能因为同一把锁失败,所以它也要能安静收场 ——
        # 一个把失败处理本身也炸掉的失败处理,等于没有失败处理。
        try:
            note_run(con, "export", started, False, 0, str(e)[:400])
            con.commit()
        except Exception:
            pass
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
    try:
        begin(con)
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
        rollback(con)
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
    else:
        # 跳过要出声, 跟上面 health 那条同一个规矩。--skip-runlog 是给每小时那次用的快速档,
        # 而快速档跟"跑过了"必须长得不一样, 否则一份跳过了运行历史的报告读起来跟完整的一样。
        print("  runlog : skipped (--skip-runlog). 这是跳过,不是通过;运行历史这一轮没有更新。")

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
