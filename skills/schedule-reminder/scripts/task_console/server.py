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
import time
import webbrowser
from collections import Counter, defaultdict
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

import allowlist
import console_store
import convos
import evtlog
import freshness
import history
import maint
import memops
import repos as repos_mod
import retire as retire_mod
import selfcheck
import sysinfo
import timeline
# 这里原来是 `import norm_rc as _norm_rc_unused`,而下面又抄了一份同名实现,于是渲染侧和
# 写入侧是两份代码。它们当时逐位等价(20 个输入含各边界实测过),但任何一侧改了边界条件
# 另一侧不会跟着变,而症状是**同一个退出码在两个面板上一个算成功一个算失败**,不报警。
# 别名里那个 unused 更糟:它让读代码的人以为共享那份已经在用了。
from rcnorm import norm_rc

# 在 pythonw(GUI 子系统)下,每个控制台子程序都要新分配一个控制台。那次分配很慢,
# 而且并发时根本不成立:实测同一条 git 命令,普通 python 下几毫秒,pythonw 下单次
# 4.5 秒,四个并发全部 15 秒超时。加上这个标志之后单次降到 0.08 秒。
#
# 这个坑只在生产形态下出现,而开发期测试都是用普通 python 跑的:探针必须复现真实的
# 调用形状,否则测的是另一个程序。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


HERE = Path(__file__).resolve().parent
COLLECT = HERE / "collect.ps1"
ACT = HERE / "act.ps1"
RUNLOG = HERE / "runlog.ps1"
PAGE = HERE / "console.html"
ICON = HERE / "icon.svg"
VENDOR = HERE / "vendor"

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


def run_ps(script: Path, env_extra: dict[str, str] | None = None, timeout: int = 90,
           args: list[str] | None = None):
    """Run a PowerShell script and return (rc, stdout, stderr), stdout decoded as UTF-8.

    Decoding is pinned rather than left to the locale: PowerShell 5.1 in a non-interactive session
    emits the ANSI codepage by default, and a category label in Chinese comes back as mojibake that
    then renders as garbage in the page. collect.ps1 pins its side too.
    """
    env = dict(os.environ)
    env.update(env_extra or {})
    p = subprocess.run(
        [powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script)] + list(args or []),
        capture_output=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace").strip(),
            p.stderr.decode("utf-8", "replace").strip())


# --------------------------------------------------------------------------- run log
# 运行日志的读取窗口与条数上限。**两条通路必须用同一份**:
# 这里以前快路写死 evtlog.read(days=30),而慢路 run_ps(RUNLOG) 吃的是 runlog.ps1 自己的
# 默认 `-Days 60`,返回的却都标注「最近 30 天」。走回落时页面上那句口径说明少报一半的窗口,
# 而没有任何一处能看出自己走的是哪条通路。
RUNLOG_DAYS = 30
# 上限从 2 万提到 10 万。2 万是个凭感觉的数,实测下来它只覆盖 **3 天**
# (2026-09-06 到今天),而返回的口径写着「最近 30 天」—— 一个七天没跑的任务
# 在这条通路上看起来就像从来没跑过。
# 实测(EvtQuery,本机):上限 2 万 -> 20000 条 / 1.8s / 截断;
# 上限 10 万 -> 54447 条 / 7.4s / 不截断,而 54450 就是这个通道当前保有的全部。
# 也就是说 10 万在这台机器上等于「有多少读多少」,代价是回落路径上多几秒,
# 而那条路本来就有 180 秒预算,并且只在数据库不可用时才走。
RUNLOG_MAX_EVENTS = 100000


def load_runlog() -> dict:
    """Aggregate the Task Scheduler Operational log into per-task run counts.

    This is REAL run history: one row per start, per completion, per action return code. It is a
    different measurement from the health monitor's poll series and is never merged into it, because
    merging them would produce a number that is neither.

    The log only goes back to the day it was enabled. An empty result therefore means NOT RECORDED
    YET, and is reported that way rather than as zero runs.
    """
    # 优先走 EvtQuery。同一批 200 条事件,PowerShell 那条路取出来约 5.5 秒、再碰一次
    # 显示名又要 5.1 秒;EvtQuery 取出来加渲染一共 2 毫秒。差三个数量级,慢的不是日志,
    # 是每条事件都去解析一次 provider 的元数据。这个差距不是优化是可行性:整段日志
    # 曾经要十几分钟才读得完,于是它被关掉了,于是「这个任务到底跑没跑成」一直是空的。
    #
    # 读不到就明说并回落,不装作成功:一段空的运行日志和一段读不到的运行日志,
    # 在界面上长得一模一样。
    raw = None
    fast_why = None
    try:
        fast = evtlog.read(days=RUNLOG_DAYS, max_events=RUNLOG_MAX_EVENTS)
        if fast.get("enabled"):
            raw = fast
        else:
            fast_why = fast.get("reason")
    except Exception as e:
        fast_why = f"{type(e).__name__}: {e}"

    if raw is None:
        # 天数与上限显式传进去,不吃脚本自己的默认值:
        # 一个「两边各有一套默认值」的接口,迟早会在某次改动后悄悄分叉。
        rc, out, err = run_ps(RUNLOG, timeout=180,
                              args=["-Days", str(RUNLOG_DAYS),
                                    "-MaxEvents", str(RUNLOG_MAX_EVENTS)])
        if rc != 0 or not out:
            return {"available": False,
                    "reason": f"读运行日志失败: {err or out or '无输出'}"
                              + (f"(快路不可用: {fast_why})" if fast_why else ""),
                    "tasks": {}}
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
    # count 必须带着它的窗口一起给。同一个界面元素在有数据库时是「run_event 全表行数」、
    # 在回落时是「最近 N 天的事件条数」,而表里的「实跑」「实成功%」永远只统计另一个窗口:
    # 三个窗口、一个数字、页面不说是哪一个。想用它判断「日志覆盖了多久」的人会得到一个偏大的数,
    # 并据此相信历史比实际更完整。
    # 撞上条数上限时,**取到的是最近 N 条,不是最近 30 天**。原来无论如何都标「最近 30 天」,
    # 于是一份只覆盖到十几天前的数据会被读成整整 30 天的完整历史,
    # 而「某个任务这段时间一次都没跑」和「它跑过但被截掉了」在屏幕上一模一样。
    truncated = bool(raw.get("truncated"))
    _oldest = raw.get("oldest")
    if truncated:
        scope = (f"最近 {RUNLOG_MAX_EVENTS} 条(撞上条数上限,没有覆盖满 {RUNLOG_DAYS} 天"
                 + (f",最早只到 {str(_oldest)[:10]}" if _oldest else "") + ")")
    else:
        # 没截断也不等于覆盖满了那个窗口:这个通道是滚动缓冲,本机实测只保有约八天。
        # 把真实回溯到哪天一起说出来,否则「这段时间它没跑」和「这段时间日志已经没了」
        # 又变成同一个空白。
        scope = f"最近 {RUNLOG_DAYS} 天" + (
            f"(日志只回溯到 {str(_oldest)[:10]})" if _oldest else "")
    return {"available": True, "reason": None, "since": raw.get("since"),
            "oldest": raw.get("oldest"), "count": raw.get("count", len(evs)),
            "windowDays": (None if truncated else RUNLOG_DAYS), "countScope": scope,
            "truncated": truncated,
            # 读到一半失败、以及解析不了的条数,两个都要带出去。读取器一直在数它们,
            # 而这里原来把两个数都扔了:事件格式一变、大批事件被丢掉时,页面上只会看到
            # 运行次数变少、成功率漂移,没有任何一处说明有多少条读不懂 ——
            # 一个看起来精确、实则不完整的数字,而它旁边正好还有个 count 给它背书。
            "partial": bool(raw.get("partial")),
            "dropped": int(raw.get("dropped") or 0),
            "tasks": out_tasks,
            "note": ("这一份是真实运行记录(每次启动、完成、动作返回码),和上面那个轮询观察是两回事。"
                     "它只回溯到日志被启用那天,所以空不等于没跑过,而是「还没记到」。")}


# --------------------------------------------------------------------------- database
def load_from_db():
    """Read history and run data from the console database.

    This is the whole performance story. Reading the Windows Operational event log costs 109 seconds
    end to end and used to sit on the request path, so every page load paid it. The ingester pays
    it out of band instead, and this reads the result in single-digit milliseconds.

    ⚠ 这句以前写的是「摄入器每小时付一次」。2026-09-08 核实:**没有任何计划任务在跑
    console_ingest.py** —— 任务计划、健康清单、备份白名单三处都查过,一处都没有。
    数据库里有东西,只是因为有人手动跑过。一句断言了不存在的排班的注释,会让下一个人
    把「数据停在三天前」读成「摄入器坏了」,而真相是它从来没被排过班。

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
        # ⚠ 同名不同义。这条通路上的 visibleRuns 是**运行日志里的真实启动数**(runs_by_day 求和),
        # 而慢路上的同名字段是「轮询看得见的 LastRunTime 去重数」,严重低估 ——
        # history.py 记过 967 对 13800,差一个数量级。两个量共用一个名字,而随数据下发的
        # caveat 文案只描述其中一种,所以 /api 的消费方按哪一种读都可能是错的。
        # 这里显式声明本条通路的口径,让读的人不必去猜自己拿到的是哪一个。
        htasks[task] = {
            "obs": c.get("obs", 0), "judged": c.get("judged", 0),
            "ok": c.get("ok", 0), "bad": c.get("bad", 0),
            "stale": c.get("stale", 0), "neutral": c.get("neutral", 0),
            # other 是判词表认不出来的那些。它进分母(judged = 全部 - neutral)却不出现在
            # 任何字段里,于是监控器换一种措辞之后,每一行会显示 health 0.0% 而
            # ok / bad / stale 全是 0 : **同一行里两个自称权威的数字互相矛盾,
            # 而没有任何字段说明观察去哪了**。history.py 那条通路 2026-09 就补上了这个桶,
            # 而数据库这条主通路一直没有 —— 于是那次修复在实际走的通路上完全没生效。
            "other": c.get("other", 0),
            "health": c.get("health"),
            "visibleRuns": sum((runs_by_day.get(task) or {}).values()),
            "visibleRunsScope": "runlog",  # 慢路给的是 "poll"
            "byDay": by_day.get(task, {}),
        }
    # available 原来硬编码成 True,和库里有没有行无关。文本那条路的同一个判断是相反的
    # (history.load 在一条都没解析出来时返回 available=False 加一句明确的原因)。
    # 于是同一个事实(没有可用的观察序列)在两条代码路径上得到相反的答案,而页面只信 available:
    # 一个只有 schema 没有数据的库,会让热力图走正常分支画一张空表、健康% 全是「-」,
    # 而没有任何一处说「库里还没有观察数据」。
    #
    # 判据用**窗口内实际拿到的天数**,不用全表行数:全表有行但最近 45 天为空,
    # 同样是「这张图没东西可画」,而那时摄入器很可能已经停了。
    if not all_days:
        _rows = cov["health"]["rows"]
        _why = ("数据库里还没有观察数据,跑一次 console_ingest.py --backfill"
                if not _rows else
                f"最近这个窗口内没有观察(全表 {_rows} 行,最后一条在 "
                f"{cov['health'].get('to') or '未知'}),摄入器可能已经停了")
        hist = {"available": False, "reason": _why, "source": "db",
                "matched": _rows, "skipped": 0, "days": [], "tasks": {},
                "lastIngest": cov.get("lastIngest")}
    else:
      hist = {
        "available": True, "reason": None, "source": "db",
        # lastIngest 原来算完就被丢掉,而它正是区分「摄入器挂了」和「本来就没跑过」的唯一信号。
        "lastIngest": cov.get("lastIngest"),
        "matched": cov["health"]["rows"], "skipped": 0, "days": all_days, "tasks": htasks,
        "caveat": ("健康率来自每小时轮询的观察序列,不是每次运行的成功率:一个坏了一整天的任务贡献约 24 条"
                   "不健康观察而不是 1 条。「实成功率」那一列才是每次运行的,来自 Windows 运行日志。"),
    }
    runs = {
        "available": cov["runs"]["rows"] > 0, "reason": None,
        "since": cov["runs"]["from"], "oldest": cov["runs"]["from"],
        "count": cov["runs"]["rows"], "tasks": rtot,
        # 数据库那条路的 count 是全表行数,不限日期,和上面回落路径那条不是一个量。
        "windowDays": None, "countScope": "库里全部",
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
    # ⚠ 这一行以前会**静默吃掉**两种条目:漏写 name 的直接丢,重名的后者覆盖前者,
    # 两种都不计数、不产生任何 warning。而「产物新鲜度覆盖率」这个数
    # (freshness.py 里 coverage = judged/total)算的是**过滤之后**那份列表,
    # 被吃掉的那部分同时从分子和分母里消失 —— 于是覆盖率对「清单条目被吃掉」
    # 这件事完全免疫,永远掉不下来。
    # 屏幕上:清单里写了 12 个、有一条把 "name" 打成了 "task",页面显示
    # 「0 · 覆盖 100% · 共 11」,绿色;那个任务的产物一个月不更新也永远不会出现在
    # 「要人管的事」里,而 100% 恰恰是本该喊出「我没查全」的那个数字。
    raw_tasks = d.get("tasks", [])
    groups: dict[str, list] = {}
    unnamed = 0
    for t in raw_tasks:
        if not isinstance(t, dict) or not t.get("name"):
            unnamed += 1
            continue
        groups.setdefault(t["name"], []).append(t)

    out = {n: (g[0] if len(g) == 1 else _merge_decls(g)) for n, g in groups.items()}

    warn = None
    multi = {n: len(g) for n, g in groups.items() if len(g) > 1}
    if unnamed or multi:
        bits = []
        if unnamed:
            bits.append(f"{unnamed} 条没有 name,被丢掉了")
        if multi:
            bits.append("同名多条(已按最严的那条合并): "
                        + ", ".join(f"{n}x{k}" for n, k in sorted(multi.items())[:5]))
        warn = (f"健康监控清单声明了 {len(raw_tasks)} 条,归到 {len(out)} 个任务名 —— "
                + ";".join(bits) + "。")
    return out, warn


# 同一个任务名在清单里可以有多条声明:一个任务把几件事折叠进来之后,
# 每件事各写一条,而监控器是 `foreach ($entry in $cfg.tasks)` **逐条**评估的。
# ⚠ 控制台这边原来是 `{t["name"]: t for ...}`,**只留下最后一条** ——
# 于是它对同一个任务显示的阈值和产物,可能比监控器实际执行的那套**更松**:
# 实测本机有一个任务写了三条(26h/26h/48h),控制台留下的正是 48h 那条,
# 盯的还是另一个产物。屏幕上没有任何一处显示「这里还有两条声明」。
# 合并规则一律取**更严**的那一侧:年龄上限取最小、豁免类的布尔取或
# (它们都是「更不容易被判绿」的方向)、产物列全部。宁可界面比监控器严,
# 不可比它松 —— 松的那一侧会让人以为已经查过了。
_STRICTER_MIN = ("max_age_hours", "artifact_max_age_hours", "grace_hours")
_STRICTER_OR = ("artifact_cannot_prove_success", "exit_code_is_authoritative")


def _merge_decls(decls: list[dict]) -> dict:
    out = dict(decls[0])
    for d in decls[1:]:
        for k, v in d.items():
            if k in _STRICTER_MIN:
                have = out.get(k)
                out[k] = v if have is None else min(have, v)
            elif k in _STRICTER_OR:
                out[k] = bool(out.get(k)) or bool(v)
            elif k not in out or out[k] in (None, ""):
                out[k] = v
    out["declCount"] = len(decls)
    # 产物全列出来:合并之后只显示一个,会让另外那些「监控器确实在盯」的文件
    # 从界面上整个消失。
    arts = [d.get("artifact") for d in decls if d.get("artifact")]
    if len(set(arts)) > 1:
        out["artifacts"] = arts
    labels = [d.get("label") for d in decls if d.get("label")]
    if labels:
        out["label"] = " / ".join(dict.fromkeys(labels))
    return out


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
    # 解析器只有一份(allowlist.py)。这里以前有自己的一份正则,**要求收尾括号顶格**,
    # 而 retire.py 那份允许它缩进 —— 同一个文件,一边说读不到,一边照常改写它。
    names, why = allowlist.parse_names(txt)
    if names is None:
        return None, f"{p}: {why}"
    return names, None


def status_of(t: dict) -> tuple[str, str]:
    if t["state"] == "Disabled":
        return "disabled", "已停用"
    # ⚠ 读不到任务信息**不是**「失败」。collect.ps1 以前把 Get-ScheduledTaskInfo 的异常
    # 整个吞掉,于是 rcRaw 是 None,而下面那行 `rc in ok` 为假,直接返回
    # ('bad', '失败 ' + (rcHex or '?')) —— **把「我没读到」编码成了一个确定的坏结论**。
    # 屏幕上是一个红色的「失败 ?」,人会去查一个其实没失败的任务。
    if t.get("infoError"):
        return "unknown", "信息读不到"
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


def _epoch(stamp: str | None) -> float | None:
    """collect.ps1 hands back 'yyyy-MM-dd HH:mm' local time, or null. Null means the scheduler had
    nothing to report, which is NOT the same as 'ran at the epoch', so it stays None all the way
    through rather than becoming a very confident 1970."""
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M").timestamp()
    except (TypeError, ValueError):
        return None


def build_freshness(tasks: dict, health: dict, health_reason: str | None = None) -> dict:
    """Artifact freshness for every task the health manifest declares.

    The manifest is the input, not the task list: a task nobody declared an artifact for cannot be
    judged on freshness, and saying so is the point. When the manifest itself is missing, coverage
    is 0 and the page says NOT CHECKED instead of drawing an empty green board.
    """
    if not health:
        # 把 load_health 给出的**具体**原因透传上去,不要在这里换成一句笼统的话。
        # 「清单解析失败(JSON 坏了)」和「压根没设环境变量」是两件事:前者是需要修的故障,
        # 后者是没启用。压成同一句之后,页面上没有任何办法把它们分开。
        return {"tasks": [], "summary": {"total": 0, "counts": {}, "judged": 0,
                                         "coverage": 0.0, "bad": 0},
                "reason": health_reason
                          or "没有健康清单,新鲜度这一栏是「未检查」,不是通过。"}
    rows = {}
    for name, t in tasks.items():
        rows[name] = {
            "state": t.get("state"),
            "last_rc": t.get("rcRaw"),
            "last_run": _epoch(t.get("lastRun")),
            "next_run": _epoch(t.get("nextRun")),
            "missed_runs": t.get("missedRuns") or 0,
        }
    return freshness.evaluate(list(health.values()), rows, time.time())


# 导出给页面的字段,以及**刻意不导出**的那些。两个集合加起来必须覆盖 hist / runs 里
# 写下的每一个键 : tests/test_console_security.py 直接对着这两个常量和生产者的字面量比对,
# 所以新增一个字段而忘了归类,会让测试变红,而不是让那个字段安静地到不了页面。
HISTORY_OUT = ("available", "reason", "days", "caveat", "matched", "source", "lastIngest")
HISTORY_DROP = ("tasks", "skipped")          # tasks 很大且已并进每一行;skipped 页面用不到
RUNLOG_OUT = ("available", "reason", "since", "oldest", "count", "note",
              "partial", "dropped", "truncated", "windowDays", "countScope")
RUNLOG_DROP = ("tasks",)                      # 同上,已并进每一行


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
    dup_cat: dict[str, list[str]] = {}
    descs: dict[str, str] = {}
    for c in cats:
        for n in c.get("tasks", []):
            # 分类配置是仓外的手写 JSON,复制粘贴一行就能让一个任务落在两个大类里。
            # 原来这里直接覆盖,于是同一个任务在表里出现两行、在两个大类的评分里各贡献一次分母,
            # 而顶部的「总数」按去重后的任务数算 : 同一屏上「总数 40」和「41/41 行」并存,
            # 所有数字都还在正常渲染,看不出哪一份是对的。
            # 现在只认第一次归属(让所有计数对齐),并把重复归属**说出来** ——
            # 悄悄挑一个和悄悄算两遍一样坏,区别只是坏得安静。
            if n in assigned:
                dup_cat.setdefault(n, [assigned[n]]).append(c["name"])
                continue
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
        # 两个键名都要认,而且**只能有一份表**知道它们叫什么。
        # 这里以前是 `e.get("ok_codes", [])`,只认一个名字:声明成 ok_exit_codes 的任务
        # 在这条渲染通路上被静默丢掉,而 freshness 那条认全 —— 同一个退出码,
        # 任务表判红、新鲜度判绿,同一屏两个自称权威的结论,没有任何一处对账。
        t["okCodes"] = ",".join(str(x) for x in freshness.declared_ok_codes(e)) or None
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
        rows = [tasks[n] for n in c.get("tasks", [])
                if n in tasks and assigned.get(n) == c["name"]]
        if rows:
            groups.append({"cat": c["name"], "desc": c.get("desc", ""), "rows": rows})
    if dup_cat:
        warnings.append(
            "分类配置里有任务被写进了多个大类,只认第一个:"
            + ";".join(f"{n} -> {'/'.join(cs)}" for n, cs in sorted(dup_cat.items())))

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
            # judged 是**动作返回码事件**的条数,不是旁边那一列显示的「实跑」(启动事件数)。
            # 一个多动作任务每次运行写多条 201,分母大于实跑;一个 rc 事件被日志滚动截断的任务,
            # 分母小于实跑。于是同一行里两个数看似同源实则不同分母,而页面没有任何地方能让人
            # 分辨:「实成功 100%」可能只建立在 1 个动作事件上,而旁边写着实跑 40 次。
            # judged 一起送到前端,让那一格能说出自己是拿几个样本算的。
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
            # 健康% 的分母是「本类里**有观察记录的**任务数」,而同一行另外三列的分母是
            # 「本类任务数」n,页面上原来只给出 n。一个 10 个任务、只有 1 个被轮询到且它
            # 100% 的大类会显示「数 10 · 健康 100.0(绿)」:一个被喂了几乎空输入的检查器
            # 打印出了满分绿色,而它的分母不在屏幕上任何地方。
            "healthN": len(hs),
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
    fresh = build_freshness(tasks, health, warn_health)
    return {
        "groups": groups,
        "freshness": fresh,
        "warnings": warnings,
        # ⚠ 这一层是字段白名单,它已经吃掉过三次字段:reason(空库那条判定生效了而页面上
        # 只有一个没有原因的 False)、lastIngest、countScope。每一次的表现都一样:
        # 后端算对了,页面上什么都没有,而没有任何东西报错。
        #
        # 所以现在是**显式两分**:HISTORY_OUT / RUNLOG_OUT 是导出的,
        # 对应的 _DROP 是刻意不导出的(体积大、页面用不到)。
        # 两边加起来必须覆盖生产者写下的每一个键,由 tests 断言 :
        # 新增字段忘了归类,测试会红,而不是它安静地到不了页面。
        "history": {k: hist[k] for k in HISTORY_OUT if k in hist},
        "runlog": {k: runs[k] for k in RUNLOG_OUT if k in runs},
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
    # fail-closed:空集合什么都不匹配,所以一个没走过 main() 的 Handler 会拒绝每一个请求。
    # 默认成 "*" 会让「忘了设置」和「明确允许一切」变成同一件事,而那正是这份代码
    # 在别处一直拒绝的形状。
    allowed_hosts: object = frozenset()
    # 同样是 fail-closed,而且理由和上面那三行逐字相同。
    # ⚠ 这里以前是 `token = ""`,做的正好是上面那段注释否定的那件事:
    # `_authed` 是 `compare_digest(请求头 or "", self.token)`,token 还是 "" 时,
    # 一个**根本不带这个头**的请求会得到 compare_digest("", "") → True,直接过鉴权。
    # 今天没被利用,只是因为 Host 闸恰好先开火(空集合拒掉一切)—— 也就是说令牌这道控制
    # 在「没初始化」状态下靠的是另一道控制兜底,而两道控制的默认值方向相反。
    # 任何设好 allowed_hosts 却漏设 token 的用法(测试 fixture、复用 Handler、
    # 将来在 main() 之外多一条启动路径)都会让 /api/ 全线免鉴权,而页面表现完全正常。
    # None 是一个不可能匹配的哨兵:「忘了设置」和「明确允许」永远不会是同一件事。
    token: str | None = None

    def log_message(self, fmt, *a):  # keep the console quiet; errors still surface in responses
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # 不许被别人 iframe 进去。Host 白名单防的是 DNS rebinding:攻击者把域名 rebind 到
        # 127.0.0.1 之后 Host 头对不上,于是被挡。iframe 走的是另一条路 :
        # <iframe src="http://127.0.0.1:8787/"> 发出去的 Host 就是 127.0.0.1:8787,
        # 白名单原样放行,页面正常渲染,而且它自带一枚有效令牌。
        # 攻击者不需要读到任何东西(CORS 挡得住读),只需要骗一次点击落在他知道位置的按钮上,
        # 而这一页上的按钮会真删目录、真跑计划任务。
        # frame-ancestors 是权威那一条,X-Frame-Options 给不认识 CSP 的老客户端兜底。
        self.send_header("Content-Security-Policy",
                         "frame-ancestors 'none'; default-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                         "form-action 'none'")
        self.send_header("X-Frame-Options", "DENY")
        # 页面里没有任何外链,所以 referrer 一栏也没有存在的理由。
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _drain(self):
        """把请求体读掉再回复,哪怕这次回复是拒绝。

        不读就直接关连接,客户端还在发 body 的那一侧会收到 ECONNRESET 而不是那个 403。
        表现出来就是一条偶发失败的安全测试:而一条偶发失败的安全测试比没有测试更糟:
        它会训练人把红色当噪音。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def _retire_plan(self):
        """只读预览:这次退役会改哪几处。写之前先让人看见要改什么。"""
        if not self._authed():
            self._drain()
            return self._json(403, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"bad request: {e}"})
        try:
            return self._json(200, retire_mod.plan(str(body.get("name") or ""),
                                                   str(body.get("reason") or "x")))
        except maint.Refused as e:
            return self._json(400, {"error": str(e), "code": e.code})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _maint_act(self):
        """维护动作。和 /api/act 分开是刻意的:两张动作表混在一起,加一个 skill 动作
        就等于同时扩大了任务动作的表面,而没有人会在评审时注意到这一点。"""
        if not self._authed():
            self._drain()
            return self._json(403, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"bad request: {e}"})
        try:
            return self._json(200, maint.act(str(body.get("action") or ""),
                                             str(body.get("name") or ""),
                                             body.get("arg")))
        except maint.Refused as e:
            return self._json(400, {"error": str(e), "code": e.code})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _authed(self) -> bool:
        # token 是 None 表示这个 Handler 没被初始化过 —— 那不是「令牌是空串」,是「没有令牌」,
        # 而没有令牌时唯一安全的答案是拒绝。不要试图在这里生成一个:
        # 一个自己发明令牌的鉴权函数,会让「服务起来了」和「服务起来了但谁都进不去」都消失。
        if not self.token:
            return False
        return secrets.compare_digest(self.headers.get("X-Console-Token", ""), self.token)

    def _host_ok(self) -> bool:
        """Binding 127.0.0.1 stops the network. It does not stop DNS rebinding, and rebinding is
        the attack that matters here: the token is substituted into the page at '/', so anything
        that can make a SAME-ORIGIN request to '/' simply reads the token out of the HTML and then
        has full access to the action endpoints. A Host allowlist is what closes that, because a
        rebound name never matches one of the loopback literals.

        '*' is a separate branch, not an entry in the list, so no hostname can ever be spelled in a
        way that turns the check off. A missing Host header is rejected too: absent is not allowed.
        """
        allowed = self.allowed_hosts
        if allowed == "*":
            return True
        host = self.headers.get("Host")
        if not host:
            return False
        return host.lower() in allowed

    # 两个入口都套一层兜底。没有它时,任何一个逃出去的异常由 socketserver 的
    # handle_error 打印 traceback 然后**直接关连接** —— 而 log_message 被置空,
    # 本地窗口里几乎什么都看不到,客户端拿到的是一个断掉的连接而不是一个错误。
    # 这几种都真的会发生:run_ps 的 subprocess.TimeoutExpired(枚举 90s / 动作 60s)、
    # json.loads(out)["tasks"] 的 ValueError / KeyError。
    # **一个报错方式是「连接消失」的接口,和一个挂掉的服务器长得一样。**
    def _guard(self, fn, label):
        try:
            return fn()
        except Exception as e:                        # noqa: BLE001
            try:
                return self._json(500, {"error": f"{label} 内部错误: "
                                                 f"{e.__class__.__name__}: {e}"[:400]})
            except Exception:
                # 连回话都失败时(连接已经断了)就算了,但不要再让异常继续往上跑,
                # 否则又回到「traceback 打在一个没人看的地方」那个形态。
                return None

    def do_GET(self):
        return self._guard(self._do_GET, "GET " + self.path.split("?", 1)[0])

    def do_POST(self):
        return self._guard(self._do_POST, "POST " + self.path.split("?", 1)[0])

    def _do_GET(self):
        if not self._host_ok():
            return self._json(400, {"error": "bad host"})
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
        if path == "/api/mem":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            try:
                return self._json(200, memops.read())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/sys":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            try:
                return self._json(200, sysinfo.read())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/convos":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            try:
                return self._json(200, convos.scan())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/repos":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            try:
                return self._json(200, repos_mod.scan())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if path.startswith("/vendor/"):
            # 第三方资产随仓发,不吊 CDN:这台控制台正是出事的时候要打开的,
            # 而出事的时候网络是最不该依赖的东西。
            # 这条路由刻意免令牌(<link> 标签发不了自定义头,里面也没有秘密),
            # 所以「不许爬出 vendor」是它唯一的控制,而那道控制必须在**碰文件系统之前**开火。
            #
            # 第一版只有解析后的归属检查,它确实拦得住 : 文件一个字节都没泄。
            # 但 "//host/share/x" 会让 Path.resolve() 在 Windows 上把它当 UNC,
            # 于是**在归属检查之前**先去连那台主机:实测对一个不可路由地址耗时 21.07 秒,
            # 对一台可达的攻击者主机则是一次自动的 NTLM 协商(凭据外泄),
            # 期间还占着一个 handler 线程。免令牌 + 任意主机 + 每请求 21 秒,
            # 这三样凑一起就是一个不用登录的外联与阻塞原语,而返回码始终是干净的 404。
            #
            # 所以改成先按形状拒绝、再逐段拼接,最后仍然保留归属检查兜底。
            # 形状检查的好处是它不需要知道操作系统怎么解释路径 :
            # 上一版的错误正是「让 resolve() 先替我理解这个字符串」。
            rel = unquote(path[len("/vendor/"):])
            segs = rel.split("/")
            if ("\\" in rel or rel.startswith("/") or not rel
                    or any(s in ("", ".", "..") or ":" in s for s in segs)):
                return self._json(404, {"error": "not found"})
            try:
                target = (VENDOR.joinpath(*segs)).resolve()
                target.relative_to(VENDOR.resolve())
            except (ValueError, OSError):
                return self._json(404, {"error": "not found"})
            ctype = {".css": "text/css; charset=utf-8",
                     ".js": "text/javascript; charset=utf-8"}.get(target.suffix, "text/plain")
            try:
                return self._send(200, target.read_bytes(), ctype)
            except OSError:
                return self._json(404, {"error": "not found"})
        if path in ("/favicon.svg", "/favicon.ico"):
            # --app= 窗口的任务栏图标取的就是页面 favicon,所以这不只是消掉一个 404:
            # 没有它,这个「桌面应用」在任务栏上是一张白纸。
            try:
                return self._send(200, ICON.read_bytes(), "image/svg+xml")
            except OSError:
                return self._json(404, {"error": "no icon"})
        if path == "/api/selfcheck":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            try:
                return self._json(200, selfcheck.run())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/maint":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            try:
                return self._json(200, maint.read_all())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/tasks":
            if not self._authed():
                return self._json(403, {"error": "bad token"})
            try:
                return self._json(200, build_payload())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        return self._json(404, {"error": "not found"})

    def _do_POST(self):
        if not self._host_ok():
            self._drain()
            return self._json(400, {"error": "bad host"})
        if self.path.split("?", 1)[0] == "/api/retire/plan":
            return self._retire_plan()
        if self.path.split("?", 1)[0] == "/api/maint/act":
            return self._maint_act()
        if self.path.split("?", 1)[0] != "/api/act":
            self._drain()
            return self._json(404, {"error": "not found"})
        if not self._authed():
            self._drain()
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
        # ⚠ act.ps1 一个字节都没输出时 res 是空字典,而 **err 完全不进响应**
        # (只有 JSON 解析失败那一支才用 out or err)。前端无条件读 r.message,
        # 于是右下角只弹出「<任务名>:undefined」四秒后消失,真正的错误文本
        # (解释器找不到、被 ExecutionPolicy 挡下、脚本解析失败 —— 这几种都是
        # rc!=0 且 stdout 为空、stderr 有正文)停在 server 进程里从不外传。
        # 一个报错却不说错在哪的界面,和不报错差不多。
        if not res.get("message"):
            res["message"] = (err or out or
                              (f"act.ps1 退出 {rc},而且什么都没输出"
                               if rc else "动作完成,但脚本没有回报任何信息"))
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
    # Three loopback spellings a browser can legitimately send for this port, and nothing else.
    # TASK_CONSOLE_ALLOWED_HOSTS adds names (comma separated); the single value "*" disables the
    # check entirely and is deliberately awkward to reach.
    extra = os.environ.get("TASK_CONSOLE_ALLOWED_HOSTS", "").strip()
    if extra == "*":
        Handler.allowed_hosts = "*"
        print("  ⚠ Host 校验已关闭 (TASK_CONSOLE_ALLOWED_HOSTS=*),DNS rebinding 防护失效。")
    else:
        hosts = {f"localhost:{a.port}", f"127.0.0.1:{a.port}", f"[::1]:{a.port}"}
        hosts |= {h.strip().lower() for h in extra.split(",") if h.strip()}
        Handler.allowed_hosts = hosts
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
