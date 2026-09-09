#!/usr/bin/env python3
"""运行日志摄入的通路选择。

这块存在的理由是一次真实故障:摄入器只有 PowerShell 一条路,而 `runlog.ps1 -Days 60`
在这台机器上撞 900 秒超时,于是 runlog 从 2026-09-05 起一直摄不进来。快路
(`evtlog.py` 的 EvtQuery)当时已经写好、量过、并且在 `server.py` 的读路径上跑着 ——
同一份日志同样 60 天,快路 4.6 秒读 5.3 万条,慢路读不完。**它只是没有被接到第二个调用点。**

所以这里测的不是「快路能不能读」(那是 test_evtlog.py 的事),是三件只会在接线处出错的事:

  1. 快路可用时**确实走了快路**,而且 PowerShell 一次都没被调用 ——
     一个悄悄回落到慢路的实现在功能上完全正确,只是会重新超时。
  2. 快路不可用时**确实回落**,并且结果一样能用(负对照)。
  3. 两条通路交出的键**同名同义**。摄入器拿 `raw.get("maxRecordId") or 0` 这种写法读它们,
     所以快路少给一个键不会报错,只会把「问不出来」当成一个值用下去:
     `maxRecordId` 缺失变成 0,`oldestRecordId` 缺失让**日志被清空的探测器整轮静默失效**。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import console_ingest as CI  # noqa: E402
import evtlog as E  # noqa: E402


# runlog.ps1 交出的顶层键里,摄入器真正读的那些。改动任一条通路时这张表是契约。
CONSUMED = ("enabled", "events", "maxRecordId", "oldestRecordId", "recordCount")


class FakeEvtlog:
    """一个可以被拨成「不可用」的 evtlog 替身。"""

    def __init__(self, ok=True, payload=None, boom=False):
        self._ok, self._payload, self._boom = ok, payload, boom
        self.calls = 0

    def available(self):
        return (self._ok, None if self._ok else "pywin32 不在")

    def read(self, days=30, max_events=20000):
        self.calls += 1
        if self._boom:
            raise RuntimeError("EvtQuery 炸了")
        return self._payload


def _fast_payload():
    return {"enabled": True, "reason": None, "events": [
        {"task": "AcmeTask", "id": 201, "rid": 4242, "t": "2026-09-07 08:06:02", "rc": "0"}],
        "maxRecordId": 4242, "oldestRecordId": 1, "recordCount": 4242}


def _slow_payload():
    return {"enabled": True, "events": [
        {"task": "AcmeTask", "id": 201, "rid": 4242, "t": "2026-09-07 08:06:02", "rc": "0"}],
        "maxRecordId": 4242, "oldestRecordId": 1, "recordCount": 4242}


@pytest.fixture
def no_ps(monkeypatch):
    """把 PowerShell 那条路换成一个会记账的替身,顺便让「其实还是走了慢路」无所遁形。"""
    seen = []

    def fake(script, args=None, timeout=600):
        seen.append((args, timeout))
        return 0, json.dumps(_slow_payload()), ""

    monkeypatch.setattr(CI, "run_ps", fake)
    return seen


def test_fast_path_is_used_and_powershell_is_not(monkeypatch, no_ps):
    fake = FakeEvtlog(payload=_fast_payload())
    monkeypatch.setitem(sys.modules, "evtlog", fake)
    raw, via = CI._read_runlog(60)
    assert via == "evtlog"
    assert fake.calls == 1
    # 这一条才是真正的回归断言。上面 via=="evtlog" 只说明它标了个名字。
    assert no_ps == [], "走了快路却还是调了 PowerShell"
    assert raw["events"][0]["rid"] == 4242


@pytest.mark.parametrize("evt", [
    FakeEvtlog(ok=False),               # pywin32 不在
    FakeEvtlog(boom=True),              # 快路本身抛异常
    None,                               # import evtlog 直接失败
])
def test_falls_back_to_powershell(monkeypatch, no_ps, evt):
    """负对照:快路不可用时必须真的回落,而且回落回来的东西一样能用。"""
    if evt is None:
        monkeypatch.setitem(sys.modules, "evtlog", None)  # None 会让属性访问抛
    else:
        monkeypatch.setitem(sys.modules, "evtlog", evt)
    raw, via = CI._read_runlog(60)
    assert via == "powershell"
    assert len(no_ps) == 1 and no_ps[0][0] == ["-Days", "60"]
    assert raw["events"][0]["rid"] == 4242


def test_disabled_channel_is_not_retried_on_the_slow_path(monkeypatch, no_ps):
    """快路说 enabled=False 是一个**结论**,不是「快路不可用」。

    通道被关掉时慢路只会得到同一个结论,还要多花十五分钟。这里把那个区分钉住:
    「读不出来」才回落,「读出来了,答案是通道关着」不回落。
    """
    monkeypatch.setitem(sys.modules, "evtlog", FakeEvtlog(
        payload={"enabled": False, "reason": "通道是关闭的", "events": []}))
    raw, via = CI._read_runlog(60)
    assert via == "evtlog" and raw["enabled"] is False
    assert no_ps == []


def test_ps_failure_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setitem(sys.modules, "evtlog", FakeEvtlog(ok=False))
    monkeypatch.setattr(CI, "run_ps", lambda s, args=None, timeout=600: (1, "", "拒绝访问"))
    raw, via = CI._read_runlog(60)
    assert via == "powershell" and raw["enabled"] is False
    assert "拒绝访问" in raw["reason"]


def test_bad_json_from_ps_is_reported(monkeypatch):
    monkeypatch.setitem(sys.modules, "evtlog", FakeEvtlog(ok=False))
    monkeypatch.setattr(CI, "run_ps", lambda s, args=None, timeout=600: (0, "not json", ""))
    raw, via = CI._read_runlog(60)
    assert via == "powershell" and raw["enabled"] is False
    assert "JSON" in raw["reason"]


# --------------------------------------------------------------- 两条通路的键必须对齐
def test_evtlog_read_exposes_every_key_the_ingester_consumes():
    """`evtlog.read()` 必须给全摄入器要读的键。

    这不是形式主义:摄入器用 `.get()` 读它们,少一个不会报错,只会静默降级。
    直接对着真机读一条 —— 用合成 payload 测这一条等于测我自己刚写的那张表。
    """
    ok, why = E.available()
    if not ok:
        pytest.skip(f"这台机器读不了事件日志: {why}")
    r = E.read(days=1, max_events=5)
    if not r.get("enabled"):
        pytest.skip(f"通道不可读: {r.get('reason')}")
    missing = [k for k in CONSUMED if k not in r]
    assert not missing, f"evtlog.read 少给了摄入器要读的键: {missing}"
    assert isinstance(r["maxRecordId"], int)
    # oldestRecordId / recordCount 允许是 None(问不出来),但不能不存在 ——
    # 「问不出来」和「没这个概念」在下游是两件事。
    for k in ("oldestRecordId", "recordCount"):
        assert r[k] is None or isinstance(r[k], int)


def test_log_stats_never_raises(monkeypatch):
    """问不出来就返回 (None, None),不能把摄入整轮带崩。"""
    monkeypatch.setitem(sys.modules, "win32evtlog", None)
    assert E.log_stats() == (None, None)


def test_max_record_id_is_a_number_not_a_missing_key():
    """rows 为空时 maxRecordId 必须是 0 而不是缺席。

    摄入器写的是 `raw.get("maxRecordId") or 0`,两种情况在它眼里一样;
    但这个键会原样进 runlog_ingest 表,下一轮拿来当水位线比。
    """
    ok, _ = E.available()
    if not ok:
        pytest.skip("这台机器读不了事件日志")
    # 一个不可能有事件的时间窗
    r = E.read(days=0, max_events=1)
    if r.get("enabled"):
        assert isinstance(r["maxRecordId"], int)
