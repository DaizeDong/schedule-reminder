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


def _is_no_more(e) -> bool:
    """EvtNext 读完时也会抛(ERROR_NO_MORE_ITEMS = 259),那是正常结束不是失败。
    不区分的话,每一次正常读完都会被报成「读到一半失败」,而一个天天误报的提示
    很快就会被无视。"""
    return getattr(e, "winerror", None) == 259 or "259" in str(e)


def channel_enabled() -> tuple[bool | None, str | None]:
    """通道自己说它开没开。None 表示问不出来(和「关着」不是一回事)。

    这个函数存在的理由:read() 返回的 enabled 一直只表示「EvtQuery 打得开」,
    而 runlog.ps1 里的同名键表示的是**通道配置里的 IsEnabled**,server.py 按后者的语义
    去解释前者。通道被关掉(Windows 默认就是关的)但通道文件还在时,EvtQuery 打得开、
    返回零条事件,快路报 enabled=True,于是永远不会回落到 runlog.ps1,
    那句「日志是关闭的,开启它是开始记录不是恢复记录」永远不出现。
    页面于是印出「运行日志 0」,而这正是本文件开头写的那句要防的事:
    **一条空的运行日志和一段读不到的运行日志在界面上长得一模一样。**
    """
    try:
        import win32evtlog
        cfg = win32evtlog.EvtOpenChannelConfig(CHANNEL)
        val = win32evtlog.EvtGetChannelConfigProperty(
            cfg, win32evtlog.EvtChannelConfigEnabled)
        # 这个 API 在不同 pywin32 版本上要么直接给 bool,要么给 (值, 类型) 元组。
        if isinstance(val, tuple):
            val = val[0]
        return bool(val), None
    except Exception as e:
        return None, f"读不到通道配置: {e.__class__.__name__}"


def log_stats() -> tuple[int | None, int | None]:
    """(通道里最老的记录号, 通道里的记录总数)。问不出来就是 (None, None)。

    第一个数是**日志被清空的探测器**:EventRecordID 在日志被清空后从 1 重新开始,
    不看它的话,重新开始的记录号会长得像已经摄入过的旧记录号,新事件被去重逻辑整批丢掉,
    而两边的计数都还是「正常」的。摄入器把它存进 runlog_ingest,下一轮拿来比。
    """
    try:
        import win32evtlog
        h = win32evtlog.EvtOpenLog(CHANNEL, win32evtlog.EvtOpenChannelPath)
        out = []
        for prop in (win32evtlog.EvtLogOldestRecordNumber,
                     win32evtlog.EvtLogNumberOfLogRecords):
            v = win32evtlog.EvtGetLogInfo(h, prop)
            # 这个 API 返回 (值, 类型) 元组。
            out.append(int(v[0] if isinstance(v, tuple) else v))
        return out[0], out[1]
    except Exception:
        return None, None


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

    # 通道关着就直接说关着,不要靠「EvtQuery 打不打得开」去猜:关着的通道
    # 只要文件还在就打得开,而那时读到的零条会被上层当成「日志正常、没有记录」。
    on, why = channel_enabled()
    if on is False:
        return {"enabled": False,
                "reason": "任务运行历史日志是关闭的。开启它是开始记录,不是恢复记录。",
                "events": []}
    # on 为 None 表示问不出来。**不能当成开着**:那样就回到了原来那个把「查不成」
    # 当成「正常」的形态。让它走 PowerShell 那条慢路去拿一个权威答案。
    if on is None:
        return {"enabled": False, "reason": why or "读不到通道配置", "events": []}
    try:
        q = win32evtlog.EvtQuery(
            CHANNEL, win32evtlog.EvtQueryReverseDirection | win32evtlog.EvtQueryChannelPath,
            _xpath(WANTED, since))
    except Exception as e:
        # 日志不存在、没开启、或没权限。三种都不是「没有事件」。
        return {"enabled": False, "reason": f"打不开事件日志: {e.__class__.__name__}",
                "events": []}

    rows, dropped, broke = [], 0, None
    while len(rows) < max_events:
        try:
            batch = win32evtlog.EvtNext(q, 100)
        except Exception as e:
            # 读到一半失败和读完了在这里原来是同一个 break,于是 enabled=True、
            # count 是个偏小的确定数字、dropped=0 : 一个数了一半却报确定数字的结果,
            # 比不报还糟。现在把它说出来。
            if not _is_no_more(e):
                broke = e.__class__.__name__
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

    oldest_rid, record_count = log_stats()
    return {
        "enabled": True,
        "reason": (f"事件读到一半失败({broke}),下面的条数是不完整的" if broke else None),
        "partial": bool(broke),
        "since": since.strftime("%Y-%m-%d"),
        "oldest": rows[-1]["t"] if rows else None,
        "count": len(rows),
        # 解析不了的事件要报出来。悄悄丢掉会让「这台机器没记录」和「我读不懂它记的东西」
        # 变成同一个空列表。
        "dropped": dropped,
        "truncated": len(rows) >= max_events,
        # 下面三个只有摄入器用:去重的水位线、日志被清空的探测器、通道里的记录总数。
        # 它们和 runlog.ps1 输出里的同名键一一对应 —— 摄入器两条通路读同一组键,
        # 缺一个都会让它把「问不出来」当成一个值来用(maxRecordId 缺失会变成 0)。
        "maxRecordId": max((r["rid"] for r in rows if r.get("rid")), default=0),
        "oldestRecordId": oldest_rid,
        "recordCount": record_count,
        "events": rows,
    }
