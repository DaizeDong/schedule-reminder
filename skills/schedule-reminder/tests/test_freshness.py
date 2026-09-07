#!/usr/bin/env python3
"""freshness.evaluate 的分叉测试。

这个模块的全部价值在于**它怎么分叉**,所以每个用例都钉住一种分叉,而不是钉一个总数。
凡是曾经因此误判过的形态,下面都有一条对应的用例,注释里写明那次误判长什么样。

负对照贯穿全篇:unknown 必须与 up 分开,坏退出码默认不能被新鲜产物洗掉,
停用的任务不能因为产物陈旧被判红。任何一条被简化掉,对应用例当场变红。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import freshness as F  # noqa: E402

NOW = 1_800_000_000.0
H = 3600.0


def mk(**kw):
    d = {"name": "T", "label": "T", "max_age_hours": 24}
    d.update(kw)
    return d


def rows(**kw):
    r = {"state": "Ready", "last_rc": 0, "last_run": NOW - 1 * H,
         "next_run": NOW + 23 * H, "missed_runs": 0}
    r.update(kw)
    return {"T": r}


def ev(decl, row=None, mtime=None):
    def fake(_path):
        return (mtime, None) if mtime is not None else (None, "读不到产物: 测试注入")
    return F.evaluate([decl], row if row is not None else rows(), NOW, mtime_of=fake)["tasks"][0]


# ---------- 三档年龄 ----------

def test_fresh_artifact_is_up():
    t = ev(mk(artifact="a", artifact_max_age_hours=24), mtime=NOW - 2 * H)
    assert t["state"] == F.UP


def test_just_past_limit_is_grace_not_down():
    # 宽限期存在的理由:一个每 24h 跑一次的任务晚了十分钟不是故障。
    t = ev(mk(artifact="a", artifact_max_age_hours=24), mtime=NOW - 25 * H)
    assert t["state"] == F.GRACE


def test_far_past_limit_is_down():
    t = ev(mk(artifact="a", artifact_max_age_hours=24), mtime=NOW - 90 * H)
    assert t["state"] == F.DOWN


def test_grace_floor_is_at_least_one_hour():
    # 周期取两成会让一个 30 分钟的任务只有 6 分钟宽限,那只会制造抖动。
    t = ev(mk(artifact="a", artifact_max_age_hours=0.5), mtime=NOW - 1.2 * H)
    assert t["state"] == F.GRACE


# ---------- HRESULT 不是退出码 ----------

def test_running_is_not_a_failure():
    # 按「非零即红」去筛,一批正在运行的任务会被当成失败,而它们好好的。
    t = ev(mk(artifact="a", artifact_max_age_hours=1),
           rows(last_rc=F.RC_RUNNING), mtime=NOW - 500 * H)
    assert t["state"] == F.RUNNING


def test_never_run_is_its_own_state():
    # 一个月度任务在创建当天的触发点已过,于是首跑排在下个月。
    # 它既不是 up 也不是 down,把它归进任何一边都会误导。
    t = ev(mk(artifact="a", artifact_max_age_hours=1),
           rows(last_rc=F.RC_NOT_RUN), mtime=NOW - 500 * H)
    assert t["state"] == F.NEVER


def test_disabled_wins_over_stale_artifact():
    t = ev(mk(artifact="a", artifact_max_age_hours=1),
           rows(state="Disabled"), mtime=NOW - 9000 * H)
    assert t["state"] == F.PAUSED


# ---------- 产物不能替退出码背书 ----------

def test_fresh_artifact_does_not_clear_a_bad_rc():
    # .log 型产物通常在失败路径上照写,
    # 所以「产物新鲜」默认洗不掉坏退出码。这条一旦被简化,
    # 那种连挂五天而监控判绿的形态会立刻回来。
    t = ev(mk(artifact="a", artifact_max_age_hours=24), rows(last_rc=126), mtime=NOW - 1 * H)
    assert t["state"] == F.DOWN


def test_declared_success_only_artifact_does_clear_it():
    t = ev(mk(artifact="a", artifact_max_age_hours=24, artifact_written_only_on_success=True),
           rows(last_rc=126), mtime=NOW - 1 * H)
    assert t["state"] == F.UP


def test_authoritative_exit_code_beats_even_that():
    t = ev(mk(artifact="a", artifact_max_age_hours=24, artifact_written_only_on_success=True,
              exit_code_is_authoritative=True),
           rows(last_rc=126), mtime=NOW - 1 * H)
    assert t["state"] == F.DOWN


def test_cannot_prove_success_artifact_does_not_report_up_on_its_own():
    t = ev(mk(artifact="a", artifact_max_age_hours=24, artifact_cannot_prove_success=True),
           rows(last_rc=0, last_run=NOW - 400 * H), mtime=NOW - 1 * H)
    assert t["state"] in (F.GRACE, F.DOWN)


# ---------- 声明的退出码要被认 ----------

@pytest.mark.parametrize("key", ["ok_codes", "ok_exit_codes"])
def test_both_spellings_of_ok_codes_are_honoured(key):
    # 实际的清单里两个键名都出现过。一个只认其中一个名字的读取器
    # 会把另一批任务的声明静默丢掉,然后天天误报,而它看起来完全正常。
    t = ev(mk(artifact="a", artifact_max_age_hours=24, **{key: [2, 3]}),
           rows(last_rc=2), mtime=NOW - 1 * H)
    assert t["state"] == F.UP


def test_code_outside_the_declared_set_still_fails_loudly():
    # 上一条的负对照:声明了 ok_codes 不等于放行一切。
    t = ev(mk(artifact="a", artifact_max_age_hours=24, ok_codes=[2, 3]),
           rows(last_rc=9), mtime=NOW - 1 * H)
    assert t["state"] == F.DOWN


# ---------- 查不成必须与查过了分开 ----------

def test_unreadable_artifact_is_unknown_not_up():
    t = ev(mk(artifact="a", artifact_max_age_hours=24, max_age_hours=None), mtime=None)
    assert t["state"] == F.UNKNOWN


def test_unknown_is_not_up():
    assert F.UNKNOWN != F.UP
    assert F.worst_of(F.UP, F.UNKNOWN) == F.UNKNOWN


def test_empty_artifact_dir_is_unknown_not_1970():
    # 空目录返回 0 会被算成 1970 年,那是一个看起来非常确定的错误答案。
    listing = []
    m, why = F.newest_mtime("d", stat=lambda p: None,
                            listdir=lambda p: listing, isdir=lambda p: True)
    assert m is None and why


def test_task_absent_from_scheduler_is_unknown():
    out = F.evaluate([mk()], {}, NOW, mtime_of=lambda p: (None, "x"))
    assert out["tasks"][0]["state"] == F.UNKNOWN


# ---------- 漏跑是系统替你算好的信号 ----------

def test_missed_runs_does_not_escalate_a_disabled_task():
    # 一个停用的任务会一直累计漏跑次数,照单升级就会被判成 grace。
    # 停用的任务当然一直漏跑,把它画成告警只会造出一个永远亮着、因而没人再看的红点。
    t = ev(mk(artifact="a", artifact_max_age_hours=1),
           rows(state="Disabled", missed_runs=23), mtime=NOW - 9000 * H)
    assert t["state"] == F.PAUSED


def test_missed_runs_does_not_escalate_a_never_run_task():
    t = ev(mk(), rows(last_rc=F.RC_NOT_RUN, missed_runs=5))
    assert t["state"] == F.NEVER


def test_missed_runs_escalates_even_when_fresh():
    t = ev(mk(artifact="a", artifact_max_age_hours=24),
           rows(missed_runs=3), mtime=NOW - 1 * H)
    assert t["state"] != F.UP
    assert any("漏跑" in r for r in t["reasons"])


# ---------- 覆盖率是判据自己的体检 ----------

def test_coverage_counts_only_judged_tasks():
    decls = [mk(name="A", artifact="a", artifact_max_age_hours=24),
             mk(name="B", artifact="b", artifact_max_age_hours=24)]
    rs = {"A": rows()["T"], "B": rows()["T"]}
    out = F.evaluate(decls, rs, NOW, mtime_of=lambda p: (NOW - 1 * H, None))
    assert out["summary"]["coverage"] == 1.0


def test_nothing_judgeable_reports_zero_coverage_not_a_pass():
    # 一个被喂了空的检查器打印的绿色,和一个真没查出问题的检查器打印的绿色一模一样。
    # 这条就是用来把两者分开的。
    out = F.evaluate([mk(name="A")], {}, NOW, mtime_of=lambda p: (None, "x"))
    assert out["summary"]["coverage"] == 0.0
    assert out["summary"]["counts"].get(F.UNKNOWN) == 1


def test_summary_bad_counts_down_and_never():
    decls = [mk(name="A", artifact="a", artifact_max_age_hours=1),
             mk(name="B", artifact="b", artifact_max_age_hours=1)]
    rs = {"A": rows()["T"], "B": dict(rows()["T"], last_rc=F.RC_NOT_RUN)}
    out = F.evaluate(decls, rs, NOW, mtime_of=lambda p: (NOW - 900 * H, None))
    assert out["summary"]["bad"] == 2
