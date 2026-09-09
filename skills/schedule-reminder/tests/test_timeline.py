#!/usr/bin/env python3
"""今日时间轴的测试。

这个文件在 2026-09-08 之前根本不存在:timeline.py 有 300 多行、决定了任务分区上那张
「今天计划跑几次、实际跑了几次」的图,而它一条测试都没有。零覆盖不会以任何方式报出来,
所以下面每一条都先说清它防的是哪种**静默**失效。

这里只测纯函数(expand / occurs_today),不碰 Windows 任务计划。
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import timeline as T  # noqa: E402

NOW = datetime(2026, 9, 8, 10, 0, 0)      # 周二


def task(triggers, state="Ready"):
    return {"state": state, "triggersRaw": triggers}


# ---------- 认不出的触发器不能被整行删掉 ----------
# 白名单式的分类必须有 else 分支,否则新增一种类型的表现就是**静默消失**:
# 月度任务和 MSFT_TaskTrigger 基类(kind 为空串)原来在 occurs_today 的最后一行
# return False,于是整个任务在今日时间轴上完全不见,而且没有任何计数说
# 「有几个触发器我不认识」。这跟模块开头写的规矩正相反:
# 「一个没人能在时间轴上看到的任务,就是一个没人记得它存在的任务」。

def test_a_monthly_trigger_is_reported_not_dropped():
    got = T.expand(task([{"kind": "Monthly", "enabled": True,
                          "start": "2026-01-01T09:00:00"}]), NOW)
    assert got["unknownTriggers"] == ["Monthly"], got


def test_a_trigger_with_no_kind_is_reported_too():
    got = T.expand(task([{"kind": "", "enabled": True,
                          "start": "2026-01-01T09:00:00"}]), NOW)
    assert got["unknownTriggers"] == ["(空)"], got


def test_known_kinds_do_not_land_in_the_unknown_bucket():
    """正对照:认识的类型不能被顺手扫进兜底桶。
    没有这一条,把所有触发器都塞进 unknownTriggers 也能让上面两条通过。"""
    got = T.expand(task([{"kind": "Daily", "enabled": True,
                          "start": "2026-09-01T09:00:00"}]), NOW)
    assert got["unknownTriggers"] == []
    assert got["points"] == ["09:00"], got


def test_event_driven_kinds_still_go_to_their_own_bucket():
    got = T.expand(task([{"kind": "Logon", "enabled": True,
                          "start": "2026-01-01T00:00:00"}]), NOW)
    assert got["eventDriven"] == ["Logon"]
    assert got["unknownTriggers"] == []


# ---------- 撞上限必须说出来 ----------
# 原来撞了之后照样报一个确定的 count 和一个确定的结束时刻:一个十秒级的任务会声称
# 「00:00 到 13:53 共 5000 次」,而真实是全天 8640 次。
# **一个数了一半却报出确定数字的结果,比不报还糟。**

def test_hitting_the_tick_cap_says_so():
    got = T.expand(task([{"kind": "Time", "enabled": True,
                          "start": "2026-01-01T00:00:00", "interval": "PT10S"}]), NOW)
    span = got["spans"][0]
    assert span["truncated"] is True, span
    assert span["count"] == T.TICK_CAP
    assert span["to"] == "23:59", "撞上限时结束时刻不能报成刻度用完的那一刻"


def test_a_span_that_fits_is_not_marked_truncated():
    """正对照:没撞上限的条带不能被标成截断,否则这个标记会天天亮,
    而一个天天亮的标记等于没有标记。"""
    got = T.expand(task([{"kind": "Time", "enabled": True,
                          "start": "2026-01-01T00:00:00", "interval": "PT5M"}]), NOW)
    span = got["spans"][0]
    assert span["truncated"] is False, span
    assert span["count"] == 288, "一天 288 个五分钟"


def test_a_disabled_task_draws_nothing():
    got = T.expand(task([{"kind": "Daily", "enabled": True,
                          "start": "2026-09-01T09:00:00"}], state="Disabled"), NOW)
    assert got["points"] == [] and got["spans"] == []
    assert got.get("skipped") == "disabled"
