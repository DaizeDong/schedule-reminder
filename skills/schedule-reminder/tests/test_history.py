#!/usr/bin/env python3
"""轮询观察日志的解析。

这个文件在 2026-09-08 之前不存在:history.py 决定了任务表上「健康%」那一列,
以及整张健康热力图,而它一条测试都没有。零覆盖不会以任何方式报出来。

重点是那个**兜底桶**:判词表认不出来的行进 other,而 other 原来只进分母、
不出现在任何字段里。监控器换一种措辞(FAIL 而不是 FAILED、DEGRADED、WARN)之后,
每一行会显示 health 0.0% 而 ok / bad / stale 全是 0 :
同一行里两个自称权威的数字互相矛盾,而没有任何字段说明观察去哪了;
skipped 也不会涨,因为行本身是解析成功的。
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import history as H  # noqa: E402

TODAY = datetime.date.today().isoformat()


def write(tmp_path, *lines):
    p = tmp_path / "h.log"
    p.write_text("".join(l + chr(10) for l in lines), encoding="utf-8")
    return str(p)


def test_unknown_verdicts_land_in_a_visible_bucket(tmp_path):
    p = write(tmp_path,
              f"[{TODAY} 01:00:00]  TaskA : DEGRADED something",
              f"[{TODAY} 02:00:00]  TaskA : DEGRADED again")
    t = H.load(p)["tasks"]["TaskA"]
    assert t["other"] == 2, t
    assert t["ok"] == t["bad"] == t["stale"] == 0
    # health 仍然是 0.0:那是真的,因为没有一条判为正常。
    # 关键在于现在有一个字段能解释这 0.0 是怎么来的。
    assert t["health"] == 0.0


def test_known_verdicts_do_not_leak_into_the_bucket(tmp_path):
    """正对照:认识的判词不能被顺手扫进兜底桶,否则 other 会天天非零,
    而一个天天非零的解释字段等于没有解释。"""
    p = write(tmp_path,
              f"[{TODAY} 01:00:00]  TaskA : OK by artifact",
              f"[{TODAY} 02:00:00]  TaskA : FAILED 0x1",
              f"[{TODAY} 03:00:00]  TaskA : STALE artifact")
    t = H.load(p)["tasks"]["TaskA"]
    assert t["other"] == 0, t
    assert (t["ok"], t["bad"], t["stale"]) == (1, 1, 1), t


def test_health_is_ok_over_judged_not_over_obs(tmp_path):
    """分母是 judged(去掉中性观察),不是 obs。两者混用会让健康率随中性观察数漂移。"""
    p = write(tmp_path,
              f"[{TODAY} 01:00:00]  TaskA : OK by artifact",
              f"[{TODAY} 02:00:00]  TaskA : FAILED 0x1")
    t = H.load(p)["tasks"]["TaskA"]
    assert t["obs"] == 2 and t["judged"] == 2
    assert t["health"] == 50.0


def test_no_path_is_unavailable_with_a_reason(tmp_path):
    got = H.load(None)
    assert got["available"] is False and got.get("reason")


def test_an_unparseable_log_says_so_instead_of_reporting_zero_tasks(tmp_path):
    """一条都解析不出来时必须是 available=False 加原因,
    而不是一个「零个任务」的空结构 : 那和一台真的没有任务的机器长得一样。"""
    p = write(tmp_path, "this is not the log format at all", "nor is this")
    got = H.load(p)
    assert got["available"] is False, got
    assert got.get("reason"), got
