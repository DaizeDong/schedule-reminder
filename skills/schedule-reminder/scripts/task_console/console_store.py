"""Read side of the task-console database, plus the one place that decides where the file lives.

WHERE THE FILE LIVES, and why this module refuses rather than guesses. The database holds real-run
output: what ran on this machine and when. Under this fleet's data boundary that can never sit in
the public repo, not even gitignored, because .gitignore is advisory and `git add -f` walks straight
through it. Resolution order:

    1. TASK_CONSOLE_DB, if set. An explicit operator override.
    2. <companion>/data/task-console/console.sqlite3, where <companion> comes from the shared
       datadir resolver.
    3. UNINITIALISED. Not a fallback into the repo, not a temp file: a stated condition with
       instructions. A repo-relative fallback is how real data ends up in a public repo, and it is
       the specific defect this boundary exists to prevent.

Every resolved path is passed through datadir.assert_outside_own_repo before it is used, so a
future edit that reintroduces an in-repo default fails loudly at the point of use rather than
silently writing there.

READS DEGRADE, WRITES DO NOT. Readers open the file read-only through a URI and, when it is absent
or unreadable, return a state object carrying a Chinese sentence explaining which of the six
conditions holds. They never return zeros. "No data" and "zero runs" are different claims and only
one of them is ours to make.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = "schedule-reminder"
DB_NAME = "console.sqlite3"

# The shared resolver ships in the guards submodule. Import it by path rather than assuming it is
# importable, because its location moved on 2026-09-01 when the kit became a submodule.
_datadir = None


def _load_datadir():
    global _datadir
    if _datadir is not None:
        return _datadir
    import importlib.util
    for cand in (
        HERE.parents[3] / "guards" / "tools" / "datadir.py",   # repo_root/guards/tools
        HERE.parents[3] / "tools" / "datadir.py",              # pre-2026-09-01 vendored layout
    ):
        if cand.exists():
            spec = importlib.util.spec_from_file_location("sr_datadir", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _datadir = mod
            return mod
    _datadir = False
    return False


class DbState:
    """Why there is no database, in a form the page can render verbatim."""

    def __init__(self, code: str, message: str, path: str | None = None):
        self.code, self.message, self.path = code, message, path

    def as_dict(self):
        return {"available": False, "code": self.code, "reason": self.message, "path": self.path}


def resolve_db(create_parent: bool = False):
    """Return (Path, None) or (None, DbState). Never returns a repo-relative path."""
    env = os.environ.get("TASK_CONSOLE_DB")
    if env:
        p = Path(os.path.expanduser(env))
    else:
        dd = _load_datadir()
        if not dd:
            return None, DbState(
                "NO_RESOLVER",
                "找不到共享的 datadir 解析器(guards/tools/datadir.py)。数据库位置无法确定,"
                "而猜一个仓内路径正是数据边界要防的事,所以这里拒绝继续。")
        try:
            root = dd.resolve_data_dir(SKILL, create=create_parent)
        except Exception as e:
            return None, DbState("RESOLVER_REFUSED", f"datadir 拒绝解析: {e}")
        if not root:
            return None, DbState(
                "NO_COMPANION",
                "没有私有伴生目录,所以没有数据库。这是「未初始化」,不是「没有历史」。"
                f"建一个 {SKILL}-config 兄弟仓,里面放 data/ 即可。")
        p = Path(root) / "task-console" / DB_NAME

    dd = _load_datadir()
    if dd:
        try:
            # Fails loudly if a future edit ever points this back inside the public repo.
            dd.assert_outside_own_repo(p, SKILL)
        except Exception as e:
            return None, DbState("INSIDE_REPO", f"拒绝把数据库放在仓内: {e}", str(p))

    if create_parent:
        p.parent.mkdir(parents=True, exist_ok=True)
    return p, None


def connect_ro():
    """Read-only connection, or (None, DbState). Read-only is not a nicety: the console serves HTTP
    and must never be able to write, so a bug there cannot corrupt the ingester's file."""
    p, st = resolve_db()
    if st:
        return None, st
    if not p.exists():
        return None, DbState("NO_FILE",
                             f"数据库还没建。跑一次 console_ingest.py --backfill 就会从现有日志重建。",
                             str(p))
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1 FROM meta LIMIT 1")
        return con, None
    except sqlite3.DatabaseError as e:
        return None, DbState("CORRUPT",
                             f"数据库打不开或结构不对({e})。删掉它再跑 --backfill 可以重建,"
                             f"因为这里没有任何一行是唯一副本。", str(p))


# --------------------------------------------------------------------------- queries
def health_by_day(con, days: int = 45) -> dict:
    rows = con.execute(
        "SELECT task, day, klass, COUNT(*) n FROM health_obs "
        "WHERE day >= date('now','localtime',?) GROUP BY task, day, klass",
        (f"-{days - 1} day",)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["task"], {}).setdefault(r["day"],
                                                     {"ok": 0, "bad": 0, "stale": 0, "neutral": 0, "n": 0})
        d[r["klass"]] = d.get(r["klass"], 0) + r["n"]
        d["n"] += r["n"]
    return out


def health_by_hour(con, day_from: str, day_to: str) -> dict:
    """Hour-level detail for a drill-down window. This is the query the text-log parser could not
    answer, because it deduplicated on the hour and then discarded it."""
    rows = con.execute(
        "SELECT task, day, hour, klass, COUNT(*) n FROM health_obs "
        "WHERE day BETWEEN ? AND ? GROUP BY task, day, hour, klass",
        (day_from, day_to)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        key = f"{r['day']} {r['hour']:02d}"
        d = out.setdefault(r["task"], {}).setdefault(key,
                                                     {"ok": 0, "bad": 0, "stale": 0, "neutral": 0, "n": 0})
        d[r["klass"]] = d.get(r["klass"], 0) + r["n"]
        d["n"] += r["n"]
    return out


def health_totals(con, days: int = 45) -> dict:
    rows = con.execute(
        "SELECT task, klass, COUNT(*) n FROM health_obs "
        "WHERE day >= date('now','localtime',?) GROUP BY task, klass",
        (f"-{days - 1} day",)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["task"], {})[r["klass"]] = r["n"]
    for t, c in out.items():
        n = sum(c.values())
        judged = n - c.get("neutral", 0)
        c["obs"], c["judged"] = n, judged
        c["health"] = round(100.0 * c.get("ok", 0) / judged, 1) if judged else None
    return out


def run_totals(con, days: int = 60) -> dict:
    rows = con.execute(
        "SELECT task, event_id, rc_norm, COUNT(*) n FROM run_event "
        "WHERE day >= date('now','localtime',?) GROUP BY task, event_id, rc_norm",
        (f"-{days - 1} day",)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        t = out.setdefault(r["task"], {"starts": 0, "done": 0, "killed": 0, "failStart": 0,
                                       "timedOut": 0, "rcs": {}})
        i, n = r["event_id"], r["n"]
        if i == 100: t["starts"] += n
        elif i == 102: t["done"] += n
        elif i == 111: t["killed"] += n
        elif i == 203: t["failStart"] += n
        elif i == 329: t["timedOut"] += n
        elif i == 201 and r["rc_norm"] is not None:
            t["rcs"][str(r["rc_norm"])] = t["rcs"].get(str(r["rc_norm"]), 0) + n
    return out


def runs_today(con) -> dict:
    rows = con.execute(
        "SELECT task, substr(ts,12,5) hm FROM run_event "
        "WHERE event_id = 100 AND day = date('now','localtime') ORDER BY ts").fetchall()
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["task"], []).append(r["hm"])
    return {k: sorted(set(v)) for k, v in out.items()}


def runs_by_day(con, days: int = 60) -> dict:
    rows = con.execute(
        "SELECT task, day, COUNT(*) n FROM run_event "
        "WHERE event_id = 100 AND day >= date('now','localtime',?) GROUP BY task, day",
        (f"-{days - 1} day",)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["task"], {})[r["day"]] = r["n"]
    return out


def coverage(con) -> dict:
    """What the database actually covers, so the page can say it instead of implying it."""
    def one(sql, *a):
        r = con.execute(sql, a).fetchone()
        return dict(r) if r else {}
    h = one("SELECT MIN(day) a, MAX(day) b, COUNT(*) n FROM health_obs")
    r = one("SELECT MIN(day) a, MAX(day) b, COUNT(*) n FROM run_event")
    ing = con.execute("SELECT source, MAX(finished_at) t, ok FROM ingest_run "
                      "WHERE finished_at IS NOT NULL GROUP BY source").fetchall()
    return {
        "health": {"from": h.get("a"), "to": h.get("b"), "rows": h.get("n", 0)},
        "runs": {"from": r.get("a"), "to": r.get("b"), "rows": r.get("n", 0)},
        "lastIngest": {x["source"]: {"at": x["t"], "ok": bool(x["ok"])} for x in ing},
    }


if __name__ == "__main__":
    con, st = connect_ro()
    if st:
        print(f"[{st.code}] {st.message}")
        print(f"path: {st.path}")
        sys.exit(1)
    import json
    print(json.dumps(coverage(con), ensure_ascii=False, indent=2))
