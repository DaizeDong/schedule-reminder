#!/usr/bin/env python3
"""摄入器停摆必须被判成一个状态,不能只是一个时间戳。

顶栏原来只印 `最后摄入 09-01 10:21`。时间戳在屏幕上永远看起来正常:没有人会读一眼
再在心里减出九天。于是一个停了两周的摄入器,和一个刚跑完的,长得一模一样 ——
热力图照画,健康% 照给一个具体数字,而那些数字全部停在两周前。

这一组用例钉的是每一种「不正常」都有自己的状态,而不是被折进同一个绿色里:
never(从没摄入过)、failed(最近一次失败)、stale(超过一天)、loss(进丢失窗口)、
unknown(时间戳读不出来)。**never 和 ok 尤其不能相等** —— 一个被喂了空的判定器
打印出的绿色,跟一个真绿的判定器一模一样。

所有输入都是合成的:固定的 `NOW` 加编出来的来源名,不读这台机器上的任何东西。
"""
import datetime as dt
import os
import sys

_SCR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "task_console")
sys.path.insert(0, _SCR)

import console_store as CS  # noqa: E402

NOW = dt.datetime(2026, 3, 15, 12, 0, 0)


def at(hours_ago, ok=True):
    t = NOW - dt.timedelta(hours=hours_ago)
    return {"at": t.strftime("%Y-%m-%d %H:%M:%S"), "ok": ok}


def v(table):
    return CS.ingest_verdict(table, now=NOW)


def test_fresh_is_ok():
    assert v({"runlog": at(1), "health": at(2)})["state"] == "ok"


def test_no_records_is_never_not_ok():
    """正对照在上面。这条是它的反面:空表不许判绿。"""
    for empty in (None, {}, {"runlog": {"at": None, "ok": True}}):
        r = v(empty)
        assert r["state"] == "never", empty
        assert r["why"], "never 必须带一句能照着做的话"


def test_a_failed_last_run_wins_over_being_recent():
    """刚跑完但失败了,不是新鲜,是坏了。"""
    r = v({"runlog": at(0.5, ok=False), "health": at(0.5)})
    assert r["state"] == "failed"
    assert r["failed"] == ["runlog"]


def test_a_day_old_is_stale():
    r = v({"runlog": at(CS.INGEST_WARN_HOURS + 1), "health": at(1)})
    assert r["state"] == "stale"
    assert r["source"] == "runlog", "判定要指名是哪条流水线停了"


def test_just_under_the_warn_line_is_still_ok():
    """负对照:阈值下面必须是绿的,否则这道闸只是恒红。"""
    assert v({"runlog": at(CS.INGEST_WARN_HOURS - 0.5)})["state"] == "ok"


def test_past_the_loss_window_is_its_own_state():
    """进丢失窗口和只是过期是两件事:后者补得回来,前者补不回来。"""
    r = v({"runlog": at(CS.INGEST_LOSS_HOURS + 1)})
    assert r["state"] == "loss"
    assert "永久" in r["why"]


def test_the_oldest_pipeline_decides():
    """一条停了、其余照跑,是最难发现的形态:页面一部分数字停了,其余照常刷新。"""
    r = v({"runlog": at(200), "health": at(0.2), "convo": at(0.3)})
    assert r["state"] == "loss"
    assert r["source"] == "runlog"


def test_unreadable_timestamp_is_unknown_not_ok():
    r = v({"runlog": {"at": "not a time", "ok": True}})
    assert r["state"] == "unknown"
    assert r["ageHours"] is None


def test_a_partly_unreadable_table_still_judges_and_says_so():
    r = v({"runlog": at(2), "health": {"at": "???", "ok": True}})
    assert r["state"] == "ok"
    assert r["unreadable"] == ["health"]
    assert "health" in (r["why"] or ""), "退出评判范围的那条必须被点名"


def test_thresholds_are_ordered_and_documented():
    """阈值必须有序,而且必须写下理由 —— 一个没有理由的数字下一个人不敢动也不敢信。"""
    assert 0 < CS.INGEST_WARN_HOURS < CS.INGEST_LOSS_HOURS
    src = open(CS.__file__, encoding="utf-8").read()
    i = src.index("INGEST_WARN_HOURS")
    assert "滚动" in src[max(0, i - 900):i], "阈值上方要写清它量的是丢失风险"
