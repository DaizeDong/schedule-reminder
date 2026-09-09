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


# ---------- 每任务每小时只算一次观察 ----------
# 这条不变量用**整段文件头**声明:「某些任务每轮被写进日志两三次(多层作业按层汇报),
# 不去重会让它们的分母比别人大两三倍,健康百分比因此不可比」。
# 而这个文件里**一条用例都没有**。一个用整段文字声明、却零覆盖的不变量,
# 和一个没有被声明的不变量,在回归发生时是同一个结果 —— 只是前者读起来像已经做过了。

def _log(lines):
    return "\n".join(lines) + "\n"


def test_repeated_lines_in_the_same_hour_count_once(tmp_path):
    p = tmp_path / "monitor.log"
    p.write_text(_log([
        # 同一小时里同一个任务写了三次(多层作业按层汇报就是这个形状)
        "[2026-09-01 03:00:00]  AcmeMultiLayer : OK last 9/1/2026 3:00:00 AM",
        "[2026-09-01 03:20:00]  AcmeMultiLayer : OK last 9/1/2026 3:00:00 AM",
        "[2026-09-01 03:59:59]  AcmeMultiLayer : OK last 9/1/2026 3:00:00 AM",
        # 对照:另一个任务同一小时只写一次
        "[2026-09-01 03:00:00]  AcmeSingle : OK last 9/1/2026 3:00:00 AM",
    ]), encoding="utf-8")
    t = H.load(str(p))["tasks"]
    assert t["AcmeMultiLayer"]["obs"] == 1, (
        f"同一小时的三条被算成了 {t['AcmeMultiLayer']['obs']} 条观察 —— "
        f"这个任务的分母会比别人大三倍,健康百分比不可比")
    assert t["AcmeSingle"]["obs"] == 1
    # 正对照:两个任务的分母一样大,这正是去重要保证的那件事。
    assert t["AcmeMultiLayer"]["judged"] == t["AcmeSingle"]["judged"]


def test_different_hours_count_separately(tmp_path):
    """负对照:跨小时必须分开算。

    少了这一条,一个「每任务每天只算一次」甚至「每任务只算一次」的实现也能让上面那条通过 ——
    而那会把整条观察序列压成一个点。
    """
    p = tmp_path / "monitor.log"
    p.write_text(_log([
        "[2026-09-01 03:00:00]  AcmeMultiLayer : OK last 9/1/2026 3:00:00 AM",
        "[2026-09-01 04:00:00]  AcmeMultiLayer : OK last 9/1/2026 4:00:00 AM",
        "[2026-09-01 05:00:00]  AcmeMultiLayer : FAILED 0x1 last 9/1/2026 5:00:00 AM",
    ]), encoding="utf-8")
    t = H.load(str(p))["tasks"]["AcmeMultiLayer"]
    assert t["obs"] == 3, f"三个不同小时被压成了 {t['obs']} 条"
    assert t["ok"] == 2 and t["bad"] == 1


def test_the_first_line_is_not_lost_to_a_bom(tmp_path):
    """带 BOM 的日志,第一条观察不能丢。

    监控器写这个日志是带 BOM 的(实测)。用普通 utf-8 读会让第一行以 U+FEFF 开头,
    于是**整条序列的第一条观察静默不匹配** —— 而它同时会让 skipped +1,
    所以这条用例连带钉住「跳过的行数要被报出来」。
    """
    p = tmp_path / "monitor.log"
    p.write_bytes(b"\xef\xbb\xbf" + _log([
        "[2026-09-01 03:00:00]  AcmeFirst : OK last 9/1/2026 3:00:00 AM",
        "[2026-09-01 04:00:00]  AcmeFirst : OK last 9/1/2026 4:00:00 AM",
    ]).encode("utf-8"))
    d = H.load(str(p))
    assert d["tasks"]["AcmeFirst"]["obs"] == 2, "带 BOM 时第一条观察丢了"
    assert d["skipped"] == 0, f"有 {d['skipped']} 行没匹配上,而这份日志每一行都该匹配"
