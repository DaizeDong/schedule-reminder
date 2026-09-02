"""task-console: a local, read-and-operate view of this machine's scheduled tasks.

WHAT IT IS. A tiny stdlib-only HTTP server that renders every self-installed Windows scheduled
task, grouped into categories you define, and lets you enable, disable, run or stop one from the
page. It is the operable counterpart to a static report: a report tells you a task is dead, this
lets you restart it without going and finding the name yourself.

WHY IT IS LOCAL-ONLY AND TOKENED. It can change system state. Three controls, all of them
load-bearing rather than decorative:

  1. It binds 127.0.0.1. Not 0.0.0.0, not a hostname. Nothing off this machine can reach it.
  2. Every /api/ call must carry a token minted fresh at startup and never written to disk. Without
     it, ANY web page you had open could POST to http://127.0.0.1:<port>/api/act and disable your
     backup task, because the browser would happily attach no credentials and the server would
     happily accept. That is the whole CSRF shape, and localhost does not protect against it.
  3. The verb list is closed (enable/disable/run/stop) and the task name is passed to PowerShell
     through an ENVIRONMENT VARIABLE, never interpolated into a command string. See act.ps1.

WHAT IT DELIBERATELY DOES NOT DO. It cannot create, delete or reconfigure a task. Creating one has
a specification with six steps and three registries (see the task-creation spec); a button that
skipped them would manufacture exactly the untracked task the spec exists to prevent.

DATA BOUNDARY. This file ships in a public repo. It reads real state at runtime and holds none of
it: no snapshot is cached to disk, the category map is read from a path OUTSIDE this repo, and the
example config that ships here contains only synthetic names.

Usage:
    python server.py [--port 8787] [--no-browser]

Environment (all optional, all with defaults that are conventions rather than real data):
    TASK_CONSOLE_CATEGORIES   category map        default ~/.task-console/categories.json
    TASK_CONSOLE_HEALTH       health watch list   no default. Unset means the health-coverage
                                                  column reads NOT CHECKED.
    TASK_CONSOLE_ALLOWLIST    backup allow-list   no default. Unset means the backup-coverage
                                                  check reads NOT CHECKED, never a pass.
    TASK_CONSOLE_HISTORY      a health monitor's  no default. Unset means no heatmap and no rates,
                              log file            and the page says so rather than showing zeros.

    The tool defaults only into its OWN namespace. Wiring it to whatever else a given machine
    keeps its watch-list and allow-list in is the launcher's job, and the launcher lives on that
    machine rather than in this repo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from collections import Counter, defaultdict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import console_store
import history
import timeline
from rcnorm import norm_rc as _norm_rc_unused

HERE = Path(__file__).resolve().parent
COLLECT = HERE / "collect.ps1"
ACT = HERE / "act.ps1"
RUNLOG = HERE / "runlog.ps1"
PAGE = HERE / "console.html"

NOT_RUN, RUNNING = 0x41303, 0x41301
VERBS = ("enable", "disable", "run", "stop")


def _home(*parts: str) -> Path:
    return Path(os.path.expanduser("~")).joinpath(*parts)


def cfg_path(env: str, *default: str) -> Path | None:
    v = os.environ.get(env)
    if v:
        return Path(os.path.expanduser(v))
    return _home(*default) if default else None


def powershell() -> str:
    for c in (
        os.environ.get("TASK_CONSOLE_POWERSHELL"),
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "powershell.exe",
    ):
        if c and (os.path.isabs(c) and os.path.exists(c) or not os.path.isabs(c)):
            return c
    return "powershell.exe"


def run_ps(script: Path, env_extra: dict[str, str] | None = None, timeout: int = 90):
    """Run a PowerShell script and return (rc, stdout, stderr), stdout decoded as UTF-8.

    Decoding is pinned rather than left to the locale: PowerShell 5.1 in a non-interactive session
    emits the ANSI codepage by default, and a category label in Chinese comes back as mojibake that
    then renders as garbage in the page. collect.ps1 pins its side too.
    """
    env = dict(os.environ)
    env.update(env_extra or {})
    p = subprocess.run(
        [powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script)],
        capture_output=True, env=env, timeout=timeout,
    )
    return (p.returncode,
            p.stdout.decode("utf-8", "replace").strip(),
            p.stderr.decode("utf-8", "replace").strip())


# --------------------------------------------------------------------------- run log
def load_runlog() -> dict:
    """Aggregate the Task Scheduler Operational log into per-task run counts.

    This is REAL run history: one row per start, per completion, per action return code. It is a
    different measurement from the health monitor's poll series and is never merged into it, because
    merging them would produce a number that is neither.

    The log only goes back to the day it was enabled. An empty result therefore means NOT RECORDED
    YET, and is reported that way rather than as zero runs.
    """
    rc, out, err = run_ps(RUNLOG, timeout=180)
    if rc != 0 or not out:
        return {"available": False, "reason": f"读运行日志失败: {err or out or '无输出'}", "tasks": {}}
    try:
        raw = json.loads(out)
    except Exception as e:
        return {"available": False, "reason": f"运行日志解析失败: {e}", "tasks": {}}
    if not raw.get("enabled"):
        return {"available": False,
                "reason": (raw.get("reason") or "任务运行历史日志是关闭的") +
                          "。开启命令: wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true(需管理员)",
                "tasks": {}}
    if raw.get("reason"):
        return {"available": False, "reason": raw["reason"], "tasks": {}}

    evs = raw.get("events") or []
    # Return codes are kept as a distribution, NOT collapsed into zero/non-zero here. Some tasks
    # encode a VERDICT in the exit code rather than a failure: an exit of 2 can mean "looked, found
    # something, said so", which is a task doing its job. Those codes are declared per task in the
    # health watch list as ok_codes. Bucketing them as failures at this layer would give exactly
    # those tasks a permanent 0%, which is how a new column becomes noise on its first day. The
    # ok-code test is applied later, where the declaration is in scope.
    per: dict[str, dict] = defaultdict(lambda: {"starts": 0, "done": 0, "killed": 0,
                                                "failStart": 0, "timedOut": 0,
                                                "rcs": Counter(), "byDay": defaultdict(int),
                                                "todayRuns": []})
    today_str = date.today().isoformat()
    for e in evs:
        t = per[e["task"]]
        i = e["id"]
        if i == 100:
            t["starts"] += 1
            day = e["t"][:10]
            t["byDay"][day] += 1
            # Times of today's actual starts, so the timeline can draw them against the planned
            # marks. A planned mark with no actual run beside it is the thing worth seeing.
            if day == today_str:
                t["todayRuns"].append(e["t"][11:16])
        elif i == 102: t["done"] += 1
        elif i == 111: t["killed"] += 1
        elif i == 203: t["failStart"] += 1
        elif i == 329: t["timedOut"] += 1
        elif i == 201:
            z = norm_rc(e.get("rc"))
            if z is not None:
                t["rcs"][z] += 1

    out_tasks = {}
    for name, t in per.items():
        out_tasks[name] = {
            "starts": t["starts"], "done": t["done"], "killed": t["killed"],
            "failStart": t["failStart"], "timedOut": t["timedOut"],
            "rcs": {str(k): v for k, v in t["rcs"].items()},
            "byDay": dict(t["byDay"]),
            "todayRuns": sorted(set(t["todayRuns"])),
        }
    return {"available": True, "reason": None, "since": raw.get("since"),
            "oldest": raw.get("oldest"), "count": raw.get("count", len(evs)),
            "tasks": out_tasks,
            "note": ("这一份是真实运行记录(每次启动、完成、动作返回码),和上面那个轮询观察是两回事。"
                     "它只回溯到日志被启用那天,所以空不等于没跑过,而是「还没记到」。")}


# --------------------------------------------------------------------------- database
def load_from_db():
    """Read history and run data from the console database.

    This is the whole performance story. Reading the Windows Operational event log costs 109 seconds
    end to end and used to sit on the request path, so every page load paid it. The ingester pays it
    once an hour instead, and this reads the result in single-digit milliseconds.

    Returns (hist, runs, note) shaped EXACTLY like the file-parsing versions, because console.html
    reads seventeen keys off them and a reshape here is a silently blank page there.
    Returns (None, None, reason) when there is no database, and the caller then falls back to the
    slow path with the reason stated. Falling back silently would hide the fact that the fast path
    is not working.
    """
    con, st = console_store.connect_ro()
    if st:
        return None, None, st.message

    try:
        by_day = console_store.health_by_day(con, days=45)
        totals = console_store.health_totals(con, days=45)
        runs_by_day = console_store.runs_by_day(con, days=60)
        rtot = console_store.run_totals(con, days=60)
        today_runs = console_store.runs_today(con)
        cov = console_store.coverage(con)
    finally:
        con.close()

    all_days = sorted({d for t in by_day.values() for d in t})
    htasks = {}
    for task, c in totals.items():
        # visibleRuns keeps its old meaning: distinct LastRunTime values observed by the poll. It is
        # NOT the run count, and the page's caveat still says so. The real count now lives in runs.
        htasks[task] = {
            "obs": c.get("obs", 0), "judged": c.get("judged", 0),
            "ok": c.get("ok", 0), "bad": c.get("bad", 0),
            "stale": c.get("stale", 0), "neutral": c.get("neutral", 0),
            "health": c.get("health"),
            "visibleRuns": sum((runs_by_day.get(task) or {}).values()),
            "byDay": by_day.get(task, {}),
        }
    hist = {
        "available": True, "reason": None, "source": "db",
        "matched": cov["health"]["rows"], "skipped": 0, "days": all_days, "tasks": htasks,
        "caveat": ("健康率来自每小时轮询的观察序列,不是每次运行的成功率:一个坏了一整天的任务贡献约 24 条"
                   "不健康观察而不是 1 条。「实成功率」那一列才是每次运行的,来自 Windows 运行日志。"),
    }
    runs = {
        "available": cov["runs"]["rows"] > 0, "reason": None,
        "since": cov["runs"]["from"], "oldest": cov["runs"]["from"],
        "count": cov["runs"]["rows"], "tasks": rtot,
        "note": ("真实运行记录,来自 Windows 任务计划的运行日志,每小时由摄入器写进数据库。"
                 "⚠️ 那个日志是滚动缓冲,实测约 5 天就会覆盖,所以没被摄入的历史是永久丢失的。"),
    }
    for name, r in rtot.items():
        r["todayRuns"] = today_runs.get(name, [])
    return hist, runs, None


# --------------------------------------------------------------------------- merge
def load_categories() -> tuple[list[dict], str | None]:
    """Return (categories, warning). A missing map is a stated condition, never a silent default."""
    p = cfg_path("TASK_CONSOLE_CATEGORIES", ".task-console", "categories.json")
    if not p or not p.exists():
        return [], (f"没有分类配置({p}),所有任务归入「未分类」。"
                    f"复制仓里的 categories.example.json 过去并按你的实际任务改。")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return [], f"分类配置解析失败({p}): {e}"
    cats = raw.get("categories") if isinstance(raw, dict) else raw
    if not isinstance(cats, list) or not cats:
        return [], f"分类配置里没有 categories 数组({p})"
    return cats, None


def load_health() -> tuple[dict, str | None]:
    """No default path. Unset means NOT CHECKED, which the page renders as unknown, not as a pass."""
    p = cfg_path("TASK_CONSOLE_HEALTH")
    if not p:
        return {}, "没有设 TASK_CONSOLE_HEALTH,健康监控覆盖这一列是「未检查」,不是通过。"
    if not p.exists():
        return {}, f"健康监控清单不存在: {p}"
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as e:
        return {}, f"健康监控清单解析失败({p}): {e}"
    return {t["name"]: t for t in d.get("tasks", []) if t.get("name")}, None


def load_allowlist() -> tuple[set[str] | None, str | None]:
    """None means NOT CHECKED. An empty set would read as 'nothing is backed up', which is a
    different and much louder claim, so the two are never collapsed."""
    v = os.environ.get("TASK_CONSOLE_ALLOWLIST")
    if not v:
        return None, None
    p = Path(os.path.expanduser(v))
    if not p.exists():
        return None, f"备份 allow-list 指向的文件不存在: {p}"
    try:
        txt = p.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        return None, f"读不了备份 allow-list: {e}"
    m = re.search(r"\$TaskNames\s*=\s*@\((.*?)\n\)", txt, re.S)
    if not m:
        return None, f"在 {p} 里找不到 $TaskNames"
    names = set(re.findall(r"^\s*'([^']+)'", m.group(1), re.M))
    if not names:
        return None, f"{p} 的 $TaskNames 解析出 0 个名字,判为未检查而不是全部缺失"
    return names, None


def norm_rc(v):
    """Unwrap a Win32 code that the event log reported as an HRESULT.

    The Task Scheduler stores a plain action return code (LastTaskResult 0x2), but event 201 in the
    Operational log reports the same thing wrapped as FACILITY_WIN32: 0x80070000 | 2 = 2147942402.
    Comparing a task's declared ok_codes (small integers like 2, 3, 4) against the wrapped form
    never matches, so every verdict-encoding task would read as 0% success while looking perfectly
    Measured 2026-09-01: a task whose declared ok code was 2 arrived here as 2147942402, and its
    success rate read 0% until the unwrap was added.

    Also accepts the signed-int32 spelling of the same value, which is how some readers hand it back.
    """
    # MISSING IS NOT ZERO. Event 100 (task started) carries no ResultCode at all, and returning 0
    # for it would write a fabricated SUCCESS into a nullable column, inflating every success rate
    # by one row per start. None means "this event does not carry a return code"; the caller must
    # skip it rather than count it. Measured 2026-09-02: the previous version returned 0 for None.
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        n = int(str(v), 0)
    except (TypeError, ValueError):
        return None
    if n < 0:
        n += 1 << 32
    if 0x80070000 <= n <= 0x8007FFFF:
        return n & 0xFFFF
    return n


def status_of(t: dict) -> tuple[str, str]:
    if t["state"] == "Disabled":
        return "disabled", "已停用"
    rc = t.get("rcRaw")
    if rc == RUNNING or t["state"] == "Running":
        return "running", "常驻中"
    if rc == NOT_RUN:
        return "pending", "尚未首跑"
    ok = {0}
    for x in str(t.get("okCodes") or "").split(","):
        if x.strip():
            ok.add(int(x))
    return ("ok", "正常") if rc in ok else ("bad", "失败 " + (t.get("rcHex") or "?"))


def issues_of(t: dict, allow: set[str] | None) -> list[list[str]]:
    out: list[list[str]] = []
    if allow is not None and not t["inAllow"]:
        out.append(["bad", "不在备份清单,换机会静默丢失"])
    if t["healthKnown"] and not t["inHealth"]:
        out.append(["bad", "不在健康监控里,死了没人知道"])
    if t.get("elsewhere"):
        out.append(["info", f"由 {t['elsewhere']} 代为监视(已声明的例外)"])
    if t.get("cannotProve"):
        out.append(["info", "已核实:这个产物撑不起豁免,监控豁免已对它关闭"])
    if t.get("authoritative"):
        out.append(["info", "退出码是权威,任何产物都不得覆盖它"])
    if t["state"] != "Disabled":
        if not t["catchup"]:
            out.append(["warn", "漏火不补跑:错过的触发直接跳过"])
        if t["timeout"] in ("PT72H", "PT0S"):
            out.append(["warn", f"超时上限 {t['timeout']} 等于无限制"])
        if t["stopOnBattery"]:
            out.append(["warn", "拔电源会被中途杀掉"])
        if t["refuseOnBattery"]:
            out.append(["warn", "用电池时拒绝启动"])
    return out


def build_payload() -> dict:
    rc, out, err = run_ps(COLLECT)
    if rc != 0 or not out:
        return {"error": f"采集失败 (rc={rc}): {err or out or '无输出'}"}
    raw = json.loads(out)

    cats, warn_cat = load_categories()
    health, warn_health = load_health()
    allow, warn_allow = load_allowlist()
    warnings = [w for w in (warn_cat, warn_health, warn_allow) if w]

    assigned: dict[str, str] = {}
    descs: dict[str, str] = {}
    for c in cats:
        for n in c.get("tasks", []):
            assigned[n] = c["name"]
        # Optional per-task Chinese descriptions. The category map is machine config living outside
        # this repo, which is where a description of the operator's real automation belongs.
        for n, d in (c.get("taskDesc") or {}).items():
            if d:
                descs[n] = d

    tasks: dict[str, dict] = {}
    for t in raw["tasks"]:
        e = health.get(t["name"], {})
        t["inAllow"] = (allow is not None and t["name"] in allow)
        t["inHealth"] = bool(e)
        t["healthKnown"] = bool(health)
        t["label"] = e.get("label")
        # Chinese override first, then the task's own description. Neither is invented: if both are
        # absent the cell stays empty rather than being filled with a plausible guess.
        t["desc"] = descs.get(t["name"]) or t.get("description") or None
        t["okCodes"] = ",".join(str(x) for x in e.get("ok_codes", [])) or None
        t["artifact"] = e.get("artifact")
        t["artifactMax"] = e.get("artifact_max_age_hours")
        t["elsewhere"] = e.get("watched_elsewhere")
        t["cannotProve"] = bool(e.get("artifact_cannot_prove_success"))
        t["authoritative"] = bool(e.get("exit_code_is_authoritative"))
        k, lbl = status_of(t)
        t["sk"], t["sl"] = k, lbl
        t["issues"] = issues_of(t, allow)
        tasks[t["name"]] = t

    groups = []
    for c in cats:
        rows = [tasks[n] for n in c.get("tasks", []) if n in tasks]
        if rows:
            groups.append({"cat": c["name"], "desc": c.get("desc", ""), "rows": rows})
    orphan = [t for n, t in tasks.items() if n not in assigned]
    if orphan:
        # Surfaced as its own group rather than dropped. A task the category map forgot is exactly
        # the one nobody is looking at, so hiding it would defeat the point of the page.
        groups.append({"cat": "未分类",
                       "desc": "分类配置里没有它们。加进 categories.json,否则每次都会落在这里。",
                       "rows": orphan})

    # DATABASE FIRST. The slow path stays as a fallback rather than being deleted, because a
    # machine that has not run the ingester yet must still get a working console; but when it is
    # used, the page says so, so "fast path broken" never looks like "everything is fine".
    hist, runs, db_reason = load_from_db()
    if db_reason:
        warnings.append(f"数据库不可用,回落到直接解析日志(会慢很多):{db_reason}")
        hist = history.load(os.environ.get("TASK_CONSOLE_HISTORY"))
        if hist.get("reason"):
            warnings.append(hist["reason"])
        runs = load_runlog()
        if runs.get("reason"):
            warnings.append(runs["reason"])
    for t in tasks.values():
        t["hist"] = hist["tasks"].get(t["name"]) or None
        rr = runs["tasks"].get(t["name"])
        if rr:
            rr = dict(rr)
            # Apply the task's own declared ok_codes, the same set the health monitor honours. A run
            # whose exit code is a declared verdict counts as a run that did its job.
            okset = {0}
            for x in str(t.get("okCodes") or "").split(","):
                if x.strip():
                    okset.add(int(x))
            total = sum(rr["rcs"].values())
            good = sum(v for k, v in rr["rcs"].items()
                       if (lambda z: z is not None and z in okset)(norm_rc(k)))
            rr["judged"] = total
            rr["good"] = good
            rr["successRate"] = round(100.0 * good / total, 1) if total else None
            rr["okApplied"] = sorted(okset - {0}) or None
        t["runs"] = rr or None

    # Per-category scores, four dimensions, each already a percentage so they are commensurable.
    # A dimension with nothing to measure against is None and renders as NOT CHECKED, never as 0:
    # a zero would read as "this category scores badly", which is a claim, and we would not have
    # made it. Rendered as a grid rather than a radar because nine categories on one radar is
    # unreadable and nine small radars say less than one grid the eye can scan down a column.
    scores = []
    for g in groups:
        rows = g["rows"]
        n = len(rows)
        hs = [r["hist"]["health"] for r in rows if r.get("hist") and r["hist"]["health"] is not None]
        sched_ok = sum(1 for r in rows
                       if r["state"] == "Disabled" or not any(i[0] == "warn" for i in r["issues"]))
        scores.append({
            "cat": g["cat"],
            "n": n,
            "health": round(sum(hs) / len(hs), 1) if hs else None,
            "backup": round(100.0 * sum(1 for r in rows if r["inAllow"]) / n, 1) if (allow is not None and n) else None,
            "watched": round(100.0 * sum(1 for r in rows if r["inHealth"] or r.get("elsewhere")) / n, 1) if (health and n) else None,
            "hygiene": round(100.0 * sched_ok / n, 1) if n else None,
        })

    # Today's schedule, expanded from the structured triggers. Actual runs recorded today are
    # attached alongside the planned ones so the two can be compared on the same axis: a planned
    # mark with no actual run beside it is the thing worth seeing.
    today = date.today().isoformat()
    for name, t in tasks.items():
        rr = t.get("runs")
        if rr and rr.get("byDay"):
            t["todayRunCount"] = rr["byDay"].get(today, 0)
    tl = timeline.build(tasks, runs.get("tasks") or {})

    n_issue = sum(1 for t in tasks.values() for i in t["issues"] if i[0] in ("bad", "warn"))
    return {
        "groups": groups,
        "warnings": warnings,
        "history": {k: hist[k] for k in ("available", "days", "caveat", "matched", "source")
                    if k in hist},
        "runlog": {k: runs[k] for k in ("available", "since", "oldest", "count", "note")
                   if k in runs},
        "scores": scores,
        "timeline": tl,
        "summary": {
            "total": len(tasks),
            "bad": sum(1 for t in tasks.values() if t["sk"] == "bad"),
            "disabled": sum(1 for t in tasks.values() if t["state"] == "Disabled"),
            "issues": n_issue,
            "generated": raw["generated"],
            "allowChecked": allow is not None,
        },
    }


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "task-console"
    token = ""

    def log_message(self, fmt, *a):  # keep the console quiet; errors still surface in responses
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page never embeds anything remote except the Google Fonts stylesheet.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _authed(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Console-Token", ""), self.token)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", self.token)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/hours":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            q = parse_qs(urlparse(self.path).query)
            a = (q.get("from") or [""])[0]
            b = (q.get("to") or [""])[0]
            if not a or not b:
                return self._json(400, {"error": "need from and to as YYYY-MM-DD"})
            con, st = console_store.connect_ro()
            if st:
                return self._json(200, {"available": False, "reason": st.message})
            try:
                return self._json(200, {"available": True, "from": a, "to": b,
                                        "tasks": console_store.health_by_hour(con, a, b)})
            finally:
                con.close()
        if path == "/api/tasks":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            try:
                return self._json(200, build_payload())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/act":
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return self._json(403, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"bad request: {e}"})

        name, verb = str(req.get("name", "")), str(req.get("verb", ""))
        if verb not in VERBS:
            return self._json(400, {"error": f"verb not allowed: {verb}"})
        if not name:
            return self._json(400, {"error": "no task name"})

        # Re-enumerate and check membership rather than trusting the client's name. The page could
        # be stale, and more to the point a name that arrived over HTTP has no standing until this
        # process has seen it in the live task list itself.
        rc, out, err = run_ps(COLLECT)
        if rc != 0 or not out:
            return self._json(500, {"error": f"重新枚举失败,拒绝执行动作: {err or out}"})
        live = {t["name"] for t in json.loads(out)["tasks"]}
        if name not in live:
            return self._json(400, {"error": f"不在本机可管理的任务列表里: {name}"})

        rc, out, err = run_ps(ACT, {"TASKCONSOLE_NAME": name, "TASKCONSOLE_VERB": verb}, timeout=60)
        try:
            res = json.loads(out) if out else {}
        except Exception:
            res = {"ok": rc == 0, "message": out or err}
        res.setdefault("ok", rc == 0)
        res["name"], res["verb"] = name, verb
        return self._json(200 if res.get("ok") else 500, res)


def main() -> int:
    ap = argparse.ArgumentParser(description="local scheduled-task console")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    if os.name != "nt":
        print("task-console 只在 Windows 上有意义:它读的是 Windows 任务计划。", file=sys.stderr)
        return 2
    for f in (COLLECT, ACT, PAGE):
        if not f.exists():
            print(f"缺文件: {f}", file=sys.stderr)
            return 2

    Handler.token = secrets.token_urlsafe(24)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"task-console: {url}")
    print("  只监听 127.0.0.1。令牌每次启动重新生成,不落盘。关掉这个窗口即停止。")
    if not a.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
