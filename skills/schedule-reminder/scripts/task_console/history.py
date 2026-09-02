"""Reconstruct per-task history from a health monitor's log.

WHAT THIS IS AND IS NOT. The Windows Task Scheduler keeps no run history unless its Operational
event log is explicitly enabled, and on a machine where it was never turned on there is nothing to
recover: history starts the day you enable it. What DOES exist is the log of a health monitor that
polls every task on a fixed cadence and writes one verdict line per task per poll. That is an
OBSERVATION series, not a run series, and the difference matters enough to name every time:

  observed health   the fraction of POLLS in which a task looked healthy. A task that fails once
                    and stays broken for a day contributes ~24 unhealthy observations, not one.
                    So this measures "how much of the time it was in a bad state", which is a real
                    and useful quantity, but it is NOT a per-run success rate and must never be
                    labelled as one.

  visible runs      distinct LastRunTime values seen across polls. Those ARE real runs. But a task
                    that runs more often than the poll interval is UNDERCOUNTED, because two polls
                    an hour apart see only the most recent run between them. Measured on this
                    machine: a five-minute task showed 967 visible runs where the schedule implies
                    roughly 13800. The undercount is not a rounding error, it is an order of
                    magnitude, and any chart built on this number has to say so.

ONE OBSERVATION PER TASK PER HOUR. Some tasks are written to the log more than once per poll (a
multi-layer job reports per layer), which would silently give them a two or three times larger
denominator than everything else and make their health percentage incomparable. Observations are
therefore deduplicated on (task, hour) before anything is counted.

The log format is the only thing this module knows about the monitor:
    [YYYY-MM-DD HH:MM:SS]   <TaskName> : <verdict text>
Anything that does not match is skipped, and the count of skipped lines is reported rather than
swallowed, so a format change shows up as a number instead of as a quietly emptier chart.
"""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

LINE = re.compile(r"^\[(\d{4})-(\d{2})-(\d{2}) (\d{2}):\d{2}:\d{2}\]\s{2,}(\S[^:]*?) : (.+)$")
LASTRUN = re.compile(r"last (\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} [AP]M)")


def classify(verdict: str) -> str:
    if verdict.startswith("OK"):
        return "ok"
    if "FAILED" in verdict or "NOT REGISTERED" in verdict:
        return "bad"
    if "STALE" in verdict or "MISSING" in verdict:
        return "stale"
    if "NEUTRAL" in verdict:
        return "neutral"
    return "other"


def load(path: str | os.PathLike | None, days: int = 45) -> dict:
    """Return per-task history, or a dict carrying only a reason when there is none."""
    if not path:
        return {"available": False,
                "reason": "没有设 TASK_CONSOLE_HISTORY,所以没有历史。这是「未提供」,不是「没有问题」。",
                "tasks": {}, "days": []}
    p = Path(os.path.expanduser(str(path)))
    if not p.exists():
        return {"available": False, "reason": f"历史日志不存在: {p}", "tasks": {}, "days": []}

    cutoff = date.today() - timedelta(days=days - 1)
    seen: set[tuple[str, str, str]] = set()          # (task, day, hour) dedup, see the header
    per: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    runs: dict[str, set[str]] = defaultdict(set)
    matched = skipped = 0

    try:
        # utf-8-SIG, not utf-8. The monitor writes this log with a BOM (EF BB BF, verified on the
        # live file), and a plain utf-8 read turns the very first line into one that starts with
        # U+FEFF, so the first observation of the whole series silently fails to match LINE.
        fh = p.open(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        return {"available": False, "reason": f"读不了历史日志: {e}", "tasks": {}, "days": []}

    with fh:
        for raw in fh:
            m = LINE.match(raw.rstrip())
            if not m:
                if raw.strip():
                    skipped += 1
                continue
            y, mo, d, hh, task, verdict = m.groups()
            day = f"{y}-{mo}-{d}"
            try:
                if date(int(y), int(mo), int(d)) < cutoff:
                    continue
            except ValueError:
                skipped += 1
                continue
            task = task.strip()
            key = (task, day, hh)
            if key in seen:
                continue
            seen.add(key)
            matched += 1
            per[task][day][classify(verdict)] += 1
            lr = LASTRUN.search(verdict)
            if lr:
                runs[task].add(lr.group(1))

    if not matched:
        # An empty result and a broken parser look identical from the outside, so say which.
        return {"available": False,
                "reason": (f"历史日志里没有能解析的判定行(跳过 {skipped} 行)。"
                           f"要么格式变了,要么这个窗口内没有记录。"),
                "tasks": {}, "days": []}

    all_days = sorted({d for t in per.values() for d in t})
    out: dict[str, dict] = {}
    for task, days_map in per.items():
        tot = Counter()
        for c in days_map.values():
            tot.update(c)
        n = sum(tot.values())
        # neutral (never-run, disabled on purpose) is excluded from the denominator: counting a
        # deliberately-off task as unhealthy would drag its score down for doing what you asked.
        judged = n - tot["neutral"]
        out[task] = {
            "obs": n,
            "judged": judged,
            "ok": tot["ok"],
            "bad": tot["bad"],
            "stale": tot["stale"],
            "neutral": tot["neutral"],
            "health": round(100.0 * tot["ok"] / judged, 1) if judged else None,
            "visibleRuns": len(runs[task]),
            "byDay": {d: {"ok": c["ok"], "bad": c["bad"], "stale": c["stale"],
                          "neutral": c["neutral"], "n": sum(c.values())}
                      for d, c in days_map.items()},
        }
    return {
        "available": True,
        "reason": None,
        "source": str(p),
        "matched": matched,
        "skipped": skipped,
        "days": all_days,
        "tasks": out,
        "caveat": ("这是每小时轮询的观察序列,不是每次运行的成功率。"
                   "「健康率」= 被判为正常的轮询占比,一个坏了一整天的任务会贡献约 24 条不健康观察而不是 1 条;"
                   "「可见运行」= 观察到的不同 LastRunTime 个数,对比轮询更密的任务(PT5M/PT15M)会严重低估。"),
    }
