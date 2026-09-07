#!/usr/bin/env python3
"""事件日志解析的测试。

解析是纯函数,所以拿真实形状的事件 XML 喂它。**不自己简化 XML 结构**:
这个模块的输入是 Windows 渲染出来的东西,一个照着自己想象写的样本会在真实格式
稍有出入时继续全绿,而生产里已经一条都解析不出来。

另一半测的是「读不到」的契约:pywin32 不在、日志打不开、事件解析不了,
三种都必须和「这台机器没有事件」区分开。
"""
import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import evtlog as E  # noqa: E402

NS = "http://schemas.microsoft.com/win/2004/08/events/event"


def ev(event_id=201, record=81834, stamp="2026-09-07T08:06:02.1234567Z",
       task="\\AcmeProxyWatchdog", extra=None, drop_task=False):
    """一条真实形状的事件 XML。字段顺序和命名照 Windows 渲染出来的样子。"""
    data = ""
    if not drop_task:
        data += f'<Data Name="TaskName">{task}</Data>'
    for k, v in (extra or {}).items():
        data += f'<Data Name="{k}">{v}</Data>'
    return (
        f'<Event xmlns="{NS}"><System>'
        f'<Provider Name="Microsoft-Windows-TaskScheduler"/>'
        f'<EventID>{event_id}</EventID><Version>0</Version><Level>4</Level>'
        f'<TimeCreated SystemTime="{stamp}"/>'
        f'<EventRecordID>{record}</EventRecordID>'
        f'<Channel>Microsoft-Windows-TaskScheduler/Operational</Channel>'
        f'</System><EventData>{data}</EventData></Event>')


# ---------- 解析 ----------

def test_parses_the_five_fields_the_console_keys_on():
    r = E._parse(ev(extra={"ResultCode": "0"}))
    assert r["task"] == "AcmeProxyWatchdog"      # 前导反斜杠要去掉
    assert r["id"] == 201
    assert r["rid"] == 81834
    assert r["rc"] == "0"
    assert r["t"].startswith("2026-09-07")


def test_return_code_is_the_other_spelling_of_result_code():
    # 同一个含义在不同事件里用两个名字。只认其中一个会让一半的行丢掉返回码,
    # 而丢掉返回码的行看起来只是「没有返回码」。
    assert E._parse(ev(extra={"ReturnCode": "2147942402"}))["rc"] == "2147942402"


def test_event_without_a_task_name_is_dropped_not_half_parsed():
    # 半条记录比没有记录更糟:它会以一个空任务名混进统计。
    assert E._parse(ev(drop_task=True)) is None


def test_malformed_xml_returns_none():
    assert E._parse("<Event><broken") is None


def test_timestamps_are_converted_out_of_utc():
    """事件里是 UTC,面板上其余时间是本地时间。不转会让整条时间轴偏移。"""
    r = E._parse(ev(stamp="2026-09-07T08:06:02.0000000Z"))
    local = dt.datetime.fromisoformat("2026-09-07T08:06:02+00:00").astimezone()
    assert r["t"] == local.strftime("%Y-%m-%d %H:%M:%S")


def test_leading_backslash_is_stripped_but_inner_path_is_kept():
    r = E._parse(ev(task="\\Microsoft\\Windows\\Thing"))
    assert r["task"] == "Microsoft\\Windows\\Thing"


# ---------- XPath ----------

def test_xpath_filters_on_every_wanted_id():
    x = E._xpath(E.WANTED, None)
    for i in E.WANTED:
        assert f"EventID={i}" in x


def test_xpath_time_bound_is_utc():
    # XPath 只接受 UTC。传本地时间会静默少取或多取一段,而少取看起来只是「那天没事件」。
    since = dt.datetime(2026, 9, 1, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    x = E._xpath((100,), since)
    assert "2026-09-01T16:00:00.000Z" in x


def test_xpath_without_a_bound_has_no_time_clause():
    assert "TimeCreated" not in E._xpath((100,), None)


# ---------- 读不到必须和没有区分 ----------

def test_missing_pywin32_reports_disabled_with_a_reason(monkeypatch):
    real = __import__

    def fake(name, *a, **k):
        if name == "win32evtlog":
            raise ImportError("no module")
        return real(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake)
    ok, why = E.available()
    assert ok is False and why
    r = E.read()
    # 空事件列表 + enabled=False + 原因。不是一个看起来正常的空日志。
    assert r["enabled"] is False and r["reason"] and r["events"] == []


def test_wanted_ids_cover_both_verdict_levels():
    # 102 是任务级判决,201 是动作级的,两者可以不一致。只收其中一个会看不见那种分歧。
    assert 102 in E.WANTED and 201 in E.WANTED
    assert 111 in E.WANTED and 329 in E.WANTED     # 被杀 与 超时


@pytest.mark.skipif(not E.available()[0], reason="没有 pywin32")
def test_real_read_reports_dropped_and_truncated_honestly():
    r = E.read(days=1, max_events=25)
    assert r["enabled"] is True
    assert "dropped" in r and "truncated" in r
    if r["count"] >= 25:
        assert r["truncated"] is True
    # 解析不了的事件必须被数出来:悄悄丢掉会让「没记录」和「读不懂」变成同一个空列表。
    assert isinstance(r["dropped"], int)
