#!/usr/bin/env python3
"""运行日志报出来的窗口,必须是它真正覆盖的窗口。

`load_runlog` 无论如何都返回 `windowDays: 30 / countScope: "最近 30 天"`。两个问题叠在一起:

  1. **两条通路的窗口不是同一个数**。快路写死 `evtlog.read(days=30)`,慢路 `run_ps(RUNLOG)`
     吃的是 runlog.ps1 自己的默认 `-Days 60` —— 走回落时实际取了 60 天,标签写 30 天,
     而没有任何一处能看出自己走的是哪条。
  2. **撞上条数上限时,取到的是最近 N 条,不是最近 30 天**。本机实测:2 万条上限只覆盖到
     三天前,而标签仍然写「最近 30 天」。一个七天没跑的任务在这条通路上看起来就像从来没跑过 ——
     「这段时间它没跑」和「这段时间的数据被截掉了」在屏幕上是同一个空白。

这里钉的是「标签不许比数据更乐观」。
"""
import os
import sys

import pytest

_SCR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "task_console")
sys.path.insert(0, _SCR)

import server as S  # noqa: E402


def _raw(count, truncated, oldest="2026-09-06 11:04:29"):
    """一份形状和 evtlog.read 一致的载荷。字段名照它真实产出的那份写,
    不自己简化 —— 一个照着想象写的样本会在真实结构稍有出入时继续全绿。"""
    return {
        "enabled": True, "reason": None, "partial": False,
        "since": "2026-08-10", "oldest": oldest,
        "count": count, "dropped": 0, "truncated": truncated,
        "events": [{"task": "AcmeTask", "id": 201, "rid": i,
                    "t": "2026-09-07 08:06:02", "rc": "0"} for i in range(3)],
    }


@pytest.fixture
def fake_fast(monkeypatch):
    """把快路换掉,让整段逻辑走确定的输入。慢路一并断掉:
    这些用例要是偷偷跑去读真机日志,它们量的就不是我以为的那件事了。"""
    def _install(raw):
        monkeypatch.setattr(S.evtlog, "read", lambda **kw: raw)
        monkeypatch.setattr(S, "run_ps",
                            lambda *a, **k: pytest.fail("不该回落到 PowerShell"))
    return _install


def test_untruncated_scope_states_how_far_the_log_actually_goes_back(fake_fast):
    fake_fast(_raw(500, False, oldest="2026-09-01 21:29:41"))
    r = S.load_runlog()
    assert r["truncated"] is False
    assert r["windowDays"] == S.RUNLOG_DAYS
    # 没截断也不等于覆盖满了窗口:这个通道是滚动缓冲。真实回溯到哪天必须说出来。
    assert "2026-09-01" in r["countScope"], r["countScope"]


def test_truncated_scope_must_not_claim_the_full_window(fake_fast):
    fake_fast(_raw(S.RUNLOG_MAX_EVENTS, True, oldest="2026-09-06 11:04:29"))
    r = S.load_runlog()
    assert r["truncated"] is True
    # windowDays 是给消费方做算术用的。截断之后它不再是一个成立的天数,
    # 给一个数会让下游拿它去算「日均多少次」,而分母是错的。
    assert r["windowDays"] is None
    assert f"{S.RUNLOG_DAYS} 天" not in r["countScope"] or "没有覆盖满" in r["countScope"], \
        r["countScope"]
    assert "2026-09-06" in r["countScope"], r["countScope"]


def test_both_paths_are_asked_for_the_same_window(monkeypatch):
    """快路和慢路必须被喂同一个天数与同一个上限。

    这条不看返回值,看**调用参数** —— 缺陷本来就发生在两边各有一套默认值上,
    而两边返回的东西长得一模一样,从结果里看不出来。
    """
    seen = {}
    monkeypatch.setattr(S.evtlog, "read",
                        lambda **kw: seen.setdefault("fast", kw) and None or
                        {"enabled": False, "reason": "假装快路不可用"})
    monkeypatch.setattr(S, "run_ps",
                        lambda script, **kw: (seen.setdefault("slow", kw), (1, "", "stop"))[1])
    S.load_runlog()
    assert seen.get("fast", {}).get("days") == S.RUNLOG_DAYS
    assert seen.get("fast", {}).get("max_events") == S.RUNLOG_MAX_EVENTS
    slow_args = seen.get("slow", {}).get("args") or []
    assert "-Days" in slow_args and str(S.RUNLOG_DAYS) in slow_args, slow_args
    assert "-MaxEvents" in slow_args and str(S.RUNLOG_MAX_EVENTS) in slow_args, slow_args


def test_the_cap_actually_covers_this_machines_channel():
    """上限要够。这条会在通道涨得比上限还大时变红 —— 那时该调的是上限,不是这条用例。

    读不到事件日志就跳过:这条问的是这台机器的实际保有量。
    """
    import evtlog
    ok, why = evtlog.available()
    if not ok:
        pytest.skip(f"读不了事件日志: {why}")
    r = evtlog.read(days=S.RUNLOG_DAYS, max_events=S.RUNLOG_MAX_EVENTS)
    if not r.get("enabled"):
        pytest.skip(f"通道不可读: {r.get('reason')}")
    assert not r["truncated"], (
        f"上限 {S.RUNLOG_MAX_EVENTS} 已经盖不住这个通道了(读到 {r['count']} 条就截断,"
        f"最早只到 {r['oldest']})。窗口标签现在会如实说出来,但该考虑提高上限了。")
