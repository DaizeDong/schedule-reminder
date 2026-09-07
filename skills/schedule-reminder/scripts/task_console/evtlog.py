"""Windows 事件日志的快速读法,产出和 runlog.ps1 完全一样的行。

为什么值得单独写一份:同一批 200 条 TaskScheduler 事件,Get-WinEvent 取出来要约 5.5 秒,
再碰一次 TaskDisplayName 又要 5.1 秒;而 EvtQuery 取出来加渲染 XML 一共 2 毫秒。
差三个数量级,慢的不是日志本身,是每条事件都去解析一次 provider 的元数据。

这个差距不是优化,是可行性:摄入整段运行日志曾经要十几分钟,于是它被关掉了,
于是「这个任务到底跑没跑成」这个问题在控制台上一直是空的。

它是可选的:pywin32 不在就返回 available=False 并说明原因,调用方回落到 PowerShell。
**不装作成功**,因为一条空的运行日志和一段读不到的运行日志在界面上长得一模一样。
"""

from __future__ import annotations

import datetime as _dt
import xml.etree.ElementTree as ET

# TaskScheduler 的 Operational 日志。100=开始 102=完成 111=被杀 201=动作返回码
# 203=启动失败 329=超时。102 是任务级判决,201 是动作级的,两者可以不一致。
CHANNEL = "Microsoft-Windows-TaskScheduler/Operational"
WANTED = (100, 102, 111, 201, 203, 329)

_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"


def available() -> tuple[bool, str | None]:
    try:
        import win32evtlog  # noqa: F401
    except Exception as e:      # ImportError 或 DLL 加载失败都算不可用
        return False, f"pywin32 不可用: {e.__class__.__name__}"
    return True, None


def _xpath(ids, since: _dt.datetime | None) -> str:
    cond = " or ".join(f"EventID={i}" for i in ids)
    if since is None:
        return f"*[System[({cond})]]"
    # 事件日志的 XPath 只接受 UTC 的 ISO 时间。传本地时间会静默少取或多取一段。
    stamp = since.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return f"*[System[({cond}) and TimeCreated[@SystemTime>='{stamp}']]]"


def _parse(xml: str) -> dict | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    sysel = root.find(f"{_NS}System")
    if sysel is None:
        return None

    def txt(tag):
        el = sysel.find(f"{_NS}{tag}")
        return el.text if el is not None else None

    tc = sysel.find(f"{_NS}TimeCreated")
    stamp = tc.get("SystemTime") if tc is not None else None
    name = rc = None
    data = root.find(f"{_NS}EventData")
    if data is not None:
        for d in data.findall(f"{_NS}Data"):
            n = d.get("Name")
            if n == "TaskName":
                name = d.text
            elif n in ("ResultCode", "ReturnCode"):
                rc = d.text
    if not name or not stamp:
        return None
    try:
        # 事件里是 UTC,面板上其余时间都是本地时间,不转会让时间轴整体偏移。
        t = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    try:
        eid = int(txt("EventID") or 0)
        rid = int(txt("EventRecordID") or 0)
    except (TypeError, ValueError):
        return None
    return {"task": name.lstrip("\\"), "id": eid, "rid": rid,
            "t": t.strftime("%Y-%m-%d %H:%M:%S"), "rc": rc}


def read(days: int = 30, max_events: int = 20000, now: _dt.datetime | None = None) -> dict:
    ok, why = available()
    if not ok:
        return {"enabled": False, "reason": why, "events": []}
    import win32evtlog

    now = now or _dt.datetime.now().astimezone()
    since = now - _dt.timedelta(days=days)
    try:
        q = win32evtlog.EvtQuery(
            CHANNEL, win32evtlog.EvtQueryReverseDirection | win32evtlog.EvtQueryChannelPath,
            _xpath(WANTED, since))
    except Exception as e:
        # 日志不存在、没开启、或没权限。三种都不是「没有事件」。
        return {"enabled": False, "reason": f"打不开事件日志: {e.__class__.__name__}",
                "events": []}

    rows, dropped = [], 0
    while len(rows) < max_events:
        try:
            batch = win32evtlog.EvtNext(q, 100)
        except Exception:
            break
        if not batch:
            break
        for ev in batch:
            try:
                xml = win32evtlog.EvtRender(ev, win32evtlog.EvtRenderEventXml)
            except Exception:
                dropped += 1
                continue
            row = _parse(xml)
            if row is None:
                dropped += 1
                continue
            rows.append(row)
            if len(rows) >= max_events:
                break

    return {
        "enabled": True,
        "reason": None,
        "since": since.strftime("%Y-%m-%d"),
        "oldest": rows[-1]["t"] if rows else None,
        "count": len(rows),
        # 解析不了的事件要报出来。悄悄丢掉会让「这台机器没记录」和「我读不懂它记的东西」
        # 变成同一个空列表。
        "dropped": dropped,
        "truncated": len(rows) >= max_events,
        "events": rows,
    }
