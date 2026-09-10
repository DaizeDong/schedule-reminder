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
    con = None
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("SELECT 1 FROM meta LIMIT 1")
        return con, None
    except sqlite3.DatabaseError as e:
        # 探针抛异常时连接已经建起来了,而调用方拿到的是 (None, DbState),手上没有它。
        # 显式关掉。
        #
        # ⚠ 这**不是**在修一个泄漏。审计报过「每次请求泄漏一个句柄」,推理是通的,
        # 对抗性核证也判它成立 —— 而实测不成立:
        #     对照组(故意开 200 个文件不关):句柄 94 -> 294,差 200(方法有效)
        #     connect_ro 走坏库 200 次:      句柄 94 -> 94, 差 0
        # CPython 的引用计数在函数返回时就析构了那个局部变量,连接随之关闭。
        # 带对照组是必须的:没有它,「两边都是 0」既符合「没漏」也符合「我没测到」——
        # 第一次用 ctypes 拿句柄数时拿回来的就是 0/0,那不是结论,那是没测到。
        #
        # 留着这个 close 的理由是**不依赖引用计数的时机**:那是实现细节,
        # 而且将来只要有一条路径在异常前把连接存进了别处(重试、缓存、日志),
        # 显式关就从多余变成必须。
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
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


# 摄入器停摆多久算「要人管」。这两个数不是随手定的档位,它们量的是**丢失风险**:
# Windows 的任务运行日志是滚动缓冲,实测约 5-8 天覆盖一轮,所以没被摄入的运行历史
# 过了那个窗口就是永久没了 —— 不是「晚点补」,是补不回来。
#   24h  这台机器上任何一条自动流水线的正常间隔都远小于一天。超过一天没摄入,
#        要么排班没了,要么在连续失败。此时还追得回来,所以是「注意」不是「严重」。
#   96h  进入丢失边缘。按实测最短的那个覆盖窗口(5 天)留一天余量,过了这条线
#        就要按「已经开始丢」处理。
INGEST_WARN_HOURS = 24.0
INGEST_LOSS_HOURS = 96.0


def ingest_verdict(last_ingest: dict | None, now=None) -> dict:
    """摄入器还活着吗 —— 一个**判定**,不是一个时间戳。

    这个函数存在的理由是页面顶栏原来只印一个时间。时间戳在屏幕上永远「看起来正常」:
    没有人会读一眼 `最后摄入 09-01 10:21` 然后在心里减出九天。于是一个已经停了两周的
    摄入器,和一个刚跑完的摄入器,长得一模一样 —— 热力图照画,健康% 照给一个具体数字,
    而那些数字全部停在两周前。**一个坏掉的东西显示成正常**,正是这块面板存在的理由。

    判定取**最旧**的那一条流水线,不取最新:摄入器是几条流水线,任何一条停了,
    页面上就有一部分数字停在那一刻,而其余部分照常刷新 —— 那比整块停掉更难发现。

    `never` 和 `ok` 必须是两个不同的答案:没有任何摄入记录时,这里绝不能因为
    「没有超时的记录」而判绿。一个被喂了空的判定器,打印出的绿色跟真绿一模一样。
    """
    import datetime as _dt

    now = _dt.datetime.now() if now is None else now
    rows = [(k, v) for k, v in (last_ingest or {}).items()
            if isinstance(v, dict) and v.get("at")]
    if not rows:
        return {"state": "never", "ageHours": None, "source": None, "at": None,
                "failed": [], "why": "摄入器没有留下任何运行记录 —— 库里的数字要么是"
                                     "手动灌的,要么根本没有。跑一次 console_ingest.py。"}

    def _parse(s):
        t = str(s).strip().replace("T", " ")[:19]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return _dt.datetime.strptime(t, fmt)
            except ValueError:
                continue
        return None

    parsed = [(k, v, _parse(v["at"])) for k, v in rows]
    unreadable = [k for k, _v, t in parsed if t is None]
    parsed = [(k, v, t) for k, v, t in parsed if t is not None]
    failed = sorted(k for k, v, _t in parsed if v.get("ok") is False)

    if not parsed:
        # 时间读不出来不能当成「没超时」。读不懂的戳和一个新鲜的戳,在判定器眼里
        # 必须是两件事,否则格式一变,这道闸就永远绿着。
        return {"state": "unknown", "ageHours": None, "source": None, "at": None,
                "failed": failed,
                "why": "摄入记录的时间戳读不出来(" + "、".join(unreadable[:3])
                       + "),没法判断摄入器是不是还在跑。"}

    parsed.sort(key=lambda x: x[2])
    src, row, at = parsed[0]
    age = (now - at).total_seconds() / 3600.0

    if failed:
        state = "failed"
        why = ("摄入器最近一次运行是失败的(" + "、".join(failed)
               + ")。这些数字停在上一次成功那一刻,而页面其余部分看不出异常。")
    elif age >= INGEST_LOSS_HOURS:
        state = "loss"
        why = (f"{src} 已经 {age / 24:.1f} 天没摄入。Windows 运行日志是滚动缓冲,"
               f"实测约 5-8 天覆盖一轮,所以这段历史很可能已经永久没了。")
    elif age >= INGEST_WARN_HOURS:
        state = "stale"
        why = (f"{src} 已经 {age:.0f} 小时没摄入,页面上的运行与健康数字都停在那时候。"
               f"再拖到 {INGEST_LOSS_HOURS / 24:.0f} 天就进丢失窗口了。")
    else:
        state = "ok"
        why = None

    out = {"state": state, "ageHours": round(age, 2), "source": src,
           "at": row.get("at"), "failed": failed, "why": why}
    if unreadable:
        # 部分读不懂时判定照给,但要说清楚它是在几条里判的 —— 否则一条读不懂的流水线
        # 会静默地退出评判范围,而它恰好可能是停掉的那条。
        out["unreadable"] = unreadable
        out["why"] = ((why + " ") if why else "") + (
            "另有 " + "、".join(unreadable[:3]) + " 的时间戳读不出来,没有参与这次判断。")
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
