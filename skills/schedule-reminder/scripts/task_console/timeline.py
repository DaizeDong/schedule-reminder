"""Expand a task's triggers into the times it is due TODAY, for a 24 hour timeline.

The scheduler exposes NextRunTime, which is one point. Laying out a day needs every point, so the
triggers are expanded here from their structured form. Expanding a summarised string like
"Daily/PT5M @21:00" back into times would be guessing, which is why collect.ps1 emits the fields.

WHAT IS AND IS NOT ON THE TIMELINE. Only triggers with a clock time can be placed: Daily, Weekly,
and a one-off Time trigger, each optionally repeating on an interval. Logon, Boot, Registration,
Idle, Event and SessionStateChange triggers fire on a condition, not at an hour, and are reported
separately as event-driven rather than dropped, because a task nobody can see on the timeline is a
task nobody remembers exists.

DENSE TASKS ARE A BAND, NOT 288 DOTS. A five-minute task has 288 occurrences a day. Drawing them
individually turns the row into a solid line that says less than one bar plus a count, so a task
whose occurrences exceed a threshold is emitted as a span with a count and the interval named.

DISABLED TASKS ARE EXCLUDED. A disabled task has triggers but will not fire, and showing it as
scheduled would be stating something false about today.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

# Task Scheduler's DaysOfWeek bitmask. Sunday is bit 0, which does not match Python's Monday=0.
_DOW_BIT = {6: 1, 0: 2, 1: 4, 2: 8, 3: 16, 4: 32, 5: 64}  # python weekday() -> mask bit

_DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")

# Above this many occurrences in a day, a row is drawn as a band instead of individual marks.
DENSE_THRESHOLD = 24
# 一天最多展开这么多个刻度。撞上限本身不是错误,但**必须说出来**:
# 见下面 truncated 那一段。
TICK_CAP = 5000


def parse_duration(s: str | None) -> int | None:
    """ISO-8601 duration to seconds. PT0S means unlimited in Task Scheduler, not zero, so it is
    returned as None rather than 0: a caller that treated it as zero would draw nothing."""
    if not s:
        return None
    m = _DUR.match(s.strip())
    if not m:
        return None
    d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
    total = d * 86400 + h * 3600 + mi * 60 + sec
    return total or None


def parse_start(s: str | None) -> datetime | None:
    if not s:
        return None
    txt = s.strip()
    # Trim a timezone offset: everything here is local wall-clock, and the scheduler stores the
    # boundary in local time already. Keeping the offset would shift every row by the UTC delta.
    txt = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", txt)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            continue
    return None


def occurs_today(t: dict, today: date) -> bool:
    """Does this trigger's recurrence rule land on today at all."""
    st = parse_start(t.get("start"))
    if not st:
        return False
    if st.date() > today:
        return False
    end = parse_start(t.get("end"))
    if end and end.date() < today:
        return False

    kind = (t.get("kind") or "").lower()
    if kind == "daily":
        n = t.get("days") or 1
        return ((today - st.date()).days % max(n, 1)) == 0
    if kind == "weekly":
        mask = t.get("dow") or 0
        if not (mask & _DOW_BIT[today.weekday()]):
            return False
        n = t.get("weeks") or 1
        weeks_between = ((today - st.date()).days) // 7
        return (weeks_between % max(n, 1)) == 0
    if kind == "time":
        # A one-off, unless it repeats. A repeating one-off with no duration runs indefinitely,
        # which is how the minute-cadence tasks on this machine are actually defined.
        if t.get("interval"):
            return True
        return st.date() == today
    return False  # logon / boot / idle / event / registration: no clock time


def expand(task: dict, now: datetime | None = None) -> dict:
    """Return {'spans': [...], 'points': [...], 'eventDriven': [...]} for one task, today."""
    now = now or datetime.now()
    today = now.date()
    points: list[str] = []
    spans: list[dict] = []
    event_driven: list[str] = []
    unknown_kinds: list[str] = []

    if task.get("state") == "Disabled":
        return {"spans": [], "points": [], "eventDriven": [], "skipped": "disabled"}

    for t in task.get("triggersRaw") or []:
        if not t.get("enabled", True):
            continue
        kind = (t.get("kind") or "").lower()
        if kind in ("logon", "boot", "registration", "idle", "event", "sessionstatechange"):
            event_driven.append(t.get("kind") or "?")
            continue
        # 白名单式的分类必须有 else 分支。月度触发器,以及 MSFT_TaskTrigger 基类那种
        # kind 为空串的,原来两边都不匹配 : occurs_today 的最后一行 return False,
        # 于是整个任务在今日时间轴上**完全消失**,而且没有任何计数说「有几个我不认识」。
        # 这跟本模块开头写的规矩正相反:「一个没人能在时间轴上看到的任务,
        # 就是一个没人记得它存在的任务」。
        if kind not in ("daily", "weekly", "time"):
            unknown_kinds.append(t.get("kind") or "(空)")
            continue
        if not occurs_today(t, today):
            continue
        st = parse_start(t["start"])
        base = datetime.combine(today, time(st.hour, st.minute, st.second))
        iv = parse_duration(t.get("interval"))
        if not iv:
            points.append(base.strftime("%H:%M"))
            continue
        dur = parse_duration(t.get("duration"))
        day_end = datetime.combine(today, time(23, 59, 59))

        if dur:
            # A bounded repetition restarts from the trigger time each day it fires, so today's
            # window is [today at the boundary time, +duration].
            first, end_dt = base, min(base + timedelta(seconds=dur), day_end)
        else:
            # UNBOUNDED repetition. This is the case that was wrong: the repetition has been running
            # continuously since StartBoundary, so today it covers the WHOLE day, at a phase set by
            # that original boundary. Anchoring the band at the boundary's time-of-day truncated
            # every such task to "starts at 17:14 today", which is simply false. Measured: a PT5M
            # task whose boundary was set at 17:14 two months ago reported 82 occurrences where the
            # real number is 288, and another was cut to begin at 08:25 instead of midnight.
            # Occurrences today are every t in [00:00, 23:59] with (t - start) a multiple of iv.
            origin = parse_start(t["start"])
            day_start = datetime.combine(today, time(0, 0, 0))
            elapsed = (day_start - origin).total_seconds()
            if elapsed >= 0:
                k = -(-elapsed // iv)                      # ceil, first tick at or after midnight
                first = origin + timedelta(seconds=k * iv)
            else:
                first = origin                             # boundary is later today
            end_dt = day_end

        n = 0
        cur = first
        marks = []
        while cur <= end_dt and n < TICK_CAP:
            marks.append(cur)
            cur += timedelta(seconds=iv)
            n += 1
        # 撞上限这件事必须说出来。原来撞了之后照样报一个确定的 count 和一个确定的 to:
        # 一个十秒级的任务会声称「00:00 到 13:53 共 5000 次」,而真实是全天 8640 次。
        # **一个数了一半却报出确定数字的结果,比不报还糟**(sysinfo 模块开头写着同一句)。
        truncated = (n >= TICK_CAP and cur <= end_dt)
        if not marks:
            continue
        if len(marks) > DENSE_THRESHOLD:
            spans.append({
                "from": marks[0].strftime("%H:%M"),
                "to": (day_end if truncated else marks[-1]).strftime("%H:%M"),
                "count": len(marks),
                "truncated": truncated,
                "every": t.get("interval"),
            })
        else:
            points.extend(m.strftime("%H:%M") for m in marks)

    return {"spans": spans, "points": sorted(set(points)),
            "eventDriven": sorted(set(event_driven)),
            # 认不出的类型照样出现在行里,像 eventDriven 那样。
            "unknownTriggers": sorted(set(unknown_kinds))}


def build(tasks: dict, runs_by_task: dict | None = None, now: datetime | None = None) -> dict:
    """Timeline payload for every task, plus today's ACTUAL runs where they are known."""
    now = now or datetime.now()
    today = now.date().isoformat()
    rows = []
    for name, t in tasks.items():
        e = expand(t, now)
        actual = []
        if runs_by_task:
            r = runs_by_task.get(name) or {}
            actual = [x for x in (r.get("todayRuns") or [])]
        # unknownTriggers 也算「这一行有东西」。
        # ⚠ 这个过滤条件原来只看 points / spans / eventDriven / actual 四项,拼出来的 row
        # 里也没有 unknownTriggers 这个键 —— 而 expand() 上面那行注释白纸黑字写着
        # 「认不出的类型照样出现在行里,像 eventDriven 那样」。注释断言的行为在代码里不存在,
        # 而它声称修好的正是同一个缺陷。
        # 后果:一个只配了 Monthly 触发器(或 CimClassName 解析出空 kind)的任务,
        # points/spans/eventDriven 全空 -> 直接 continue -> **今日时间轴上一行都没有**,
        # 而页面上没有任何一处说「有 1 个触发器我不认识」。
        # 屏幕表现与「这个任务今天本来就不该跑」逐像素相同。
        unknown = e.get("unknownTriggers") or []
        if e.get("skipped") or (not e["points"] and not e["spans"]
                                and not e["eventDriven"] and not unknown and not actual):
            continue
        rows.append({
            "name": name, "cat": t.get("cat"),
            "points": e["points"], "spans": e["spans"],
            "eventDriven": e["eventDriven"], "unknownTriggers": unknown,
            "actual": actual,
            "sk": t.get("sk"),
        })
    rows.sort(key=lambda r: (r["points"][0] if r["points"]
                             else (r["spans"][0]["from"] if r["spans"] else "zz")))
    n_unknown = sum(len(r["unknownTriggers"]) for r in rows)
    return {
        "date": today,
        "now": now.strftime("%H:%M"),
        "rows": rows,
        # 认不出的触发器总数。给页面一个能印在标题上的数:
        # 一个只存在于某一行 tooltip 里的信号,和没有这个信号差别不大。
        "unknownTriggerCount": n_unknown,
        "note": ("按触发器展开的今日计划。事件驱动的触发器(登录/开机/空闲/事件)没有时钟时间,"
                 "单独标出而不是丢掉。分钟级任务画成条带加次数,288 个点连成一条实线反而什么也没说。"
                 "已停用的任务不画:它有触发器但不会触发,画上去就是对今天做了一个假陈述。"),
    }
