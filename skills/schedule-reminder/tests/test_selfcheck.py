#!/usr/bin/env python3
"""自检自己也要被检。

这个模块的全部价值是「说出它没读到什么」,所以下面的用例几乎全是负面场景:
路径不存在、类型不对、文件是空的、根本没配。每一条都必须落到一个**和 ok 不同**的状态。

最重要的一条是 test_a_broken_required_source_flips_the_overall_verdict:
如果一个必需来源坏掉之后整体判定还是 ok,那这个自检就是个摆设。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import selfcheck as SC  # noqa: E402

NOW = 1_800_000_000.0


@pytest.fixture
def bundle(tmp_path):
    """一份「装好了」的随包文件,免得每条用例都被 bundled 缺失刷屏。"""
    for _k, _t, fname in SC.BUNDLED:
        # BUNDLED 里现在有带子目录的条目(vendor/tabler/...),父目录得先建出来。
        p = tmp_path / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return tmp_path


def run(bundle, **env):
    return SC.run(now=NOW, here=bundle, env=env)


def row(res, key):
    return next(r for r in res["rows"] if r["key"] == key)


# ---------- 四态各自成立 ----------

def test_unset_source_is_unset_not_missing(bundle):
    r = row(run(bundle), "health")
    assert r["state"] == SC.UNSET
    assert r["path"] is None
    # 「我没让它检查」和「我让它检查了而它坏了」是两件事,合并会让后者失去紧急性。
    assert SC.UNSET != SC.MISSING


def test_configured_but_absent_path_is_missing(bundle, tmp_path):
    r = row(run(bundle, TASK_CONSOLE_HEALTH=str(tmp_path / "nope.json")), "health")
    assert r["state"] == SC.MISSING
    assert r["why"]


def test_present_file_is_ok(bundle, tmp_path):
    f = tmp_path / "h.json"
    f.write_text('{"tasks":[]}', encoding="utf-8")
    os.utime(f, (NOW - 60, NOW - 60))
    assert row(run(bundle, TASK_CONSOLE_HEALTH=str(f)), "health")["state"] == SC.OK


def test_empty_required_file_is_missing_not_ok(bundle, tmp_path):
    # 零字节文件读起来跟正常文件一样成功,但它什么都给不了。
    f = tmp_path / "h.json"
    f.write_text("", encoding="utf-8")
    r = row(run(bundle, TASK_CONSOLE_HEALTH=str(f)), "health")
    assert r["state"] == SC.MISSING


def test_stale_source_is_stale_not_missing_and_not_ok(bundle, tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("x", encoding="utf-8")
    os.utime(f, (NOW - 100 * 3600, NOW - 100 * 3600))   # history 声明 48h
    r = row(run(bundle, TASK_CONSOLE_HISTORY=str(f)), "history")
    assert r["state"] == SC.STALE
    assert r["ageHours"] > 48


def test_type_mismatch_is_caught_both_ways(bundle, tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    f = tmp_path / "f"
    f.write_text("x", encoding="utf-8")
    # 声明是文件却给了目录
    assert row(run(bundle, TASK_CONSOLE_HEALTH=str(d)), "health")["state"] == SC.MISSING
    # 声明是目录却给了文件
    assert row(run(bundle, TASK_CONSOLE_SKILLS=str(f)), "skills")["state"] == SC.MISSING


# ---------- 整体判定必须会翻 ----------

def test_a_broken_required_source_flips_the_overall_verdict(bundle, tmp_path):
    good = tmp_path / "h.json"
    good.write_text("{}", encoding="utf-8")
    assert run(bundle, TASK_CONSOLE_HEALTH=str(good))["ok"] is True   # 正对照
    bad = run(bundle, TASK_CONSOLE_HEALTH=str(tmp_path / "gone.json"))
    assert bad["ok"] is False
    assert "health" in bad["broken"]


def test_missing_bundled_file_flips_it_too(tmp_path):
    # 随包文件缺了是安装坏了,不是没配。它没有环境变量可以「不设」。
    for _k, _t, fname in SC.BUNDLED[1:]:
        p = tmp_path / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    health = tmp_path / "h.json"
    health.write_text("{}", encoding="utf-8")
    # 只让随包文件缺一个,别的全给对:这样翻红只可能是它翻的。
    res = SC.run(now=NOW, here=tmp_path, env={"TASK_CONSOLE_HEALTH": str(health)})
    assert res["ok"] is False
    assert res["broken"] == ["collect"]


def test_optional_unset_source_does_not_flip_it(bundle, tmp_path):
    # 负对照的反面:可选来源没配不该把整体拖红,否则这个判定天天红,等于没有判定。
    good = tmp_path / "h.json"
    good.write_text("{}", encoding="utf-8")
    res = run(bundle, TASK_CONSOLE_HEALTH=str(good))
    assert res["ok"] is True
    assert res["counts"].get(SC.UNSET, 0) > 0     # 确实有没配的
    assert not res["broken"]


# ---------- 计数是自检自己的体检 ----------

def test_probed_counts_only_sources_actually_read(bundle, tmp_path):
    good = tmp_path / "h.json"
    good.write_text("{}", encoding="utf-8")
    res = run(bundle, TASK_CONSOLE_HEALTH=str(good))
    # 没配的来源不算「探到了」。把它们算进去会让覆盖率虚高,而覆盖率虚高正是
    # 这类面板最容易骗人的地方。
    assert res["probed"] == len(SC.BUNDLED) + 1
    assert res["total"] == len(SC.SOURCES) + len(SC.BUNDLED)
    assert res["probed"] < res["total"]


def test_every_source_key_is_unique():
    keys = [s[0] for s in SC.SOURCES] + [b[0] for b in SC.BUNDLED]
    assert len(keys) == len(set(keys))


def test_every_panel_source_is_in_the_source_table():
    """页面上每一块面板的来源都必须出现在自检表里。

    这条是补一个实测漏洞:自检报「读到 12/12」的同时,页面上有三块面板显示「未检查」。
    一个漏掉了三个来源的自检打印满分,和一个真的全绿的自检长得一模一样,
    而它恰恰是整页里唯一负责回答「我到底看了多少东西」的那一块。
    """
    known = {v for _k, _t, v, _kind, _age, _req in SC.SOURCES if v}
    for var in ("TASK_CONSOLE_REPOS", "TASK_CONSOLE_PLUGIN_CACHE",
                "TASK_CONSOLE_SESSIONS", "TASK_CONSOLE_MEMORY_ARCHIVER",
                "TASK_CONSOLE_VISIBILITY", "TASK_CONSOLE_SKILLS",
                "TASK_CONSOLE_MEMORY", "TASK_CONSOLE_HEALTH"):
        assert var in known, f"{var} 是某块面板的来源,但自检不知道它存在"


# ---------- 空目录不是「ok」 ----------
# 一个空目录,和一个「配了但同步没跑 / 挂载点没挂上 / 路径改过」,在文件系统上长得一模一样,
# 而后者正是这个模块存在的理由所反对的那种绿色:自检整块打绿,对应面板显示 0 个条目。
# 空文件那一半原来已经判 missing,目录这一半却判 ok:同一个「配了但里面什么都没有」
# 给了两种结论。

def test_an_empty_directory_is_missing_not_ok(bundle, tmp_path):
    empty = tmp_path / "empty-dir"
    empty.mkdir()
    res = run(bundle, TASK_CONSOLE_SKILLS=str(empty))
    r = row(res, "skills")
    assert r["state"] == "missing", r
    assert "空" in (r.get("why") or ""), r


def test_a_directory_with_something_in_it_is_ok(bundle, tmp_path):
    """正对照:有东西的目录不能因为这次收紧就一起判红,
    否则这条检查会天天亮,而天天亮的检查等于没有。"""
    full = tmp_path / "full-dir"
    full.mkdir()
    (full / "a").mkdir()
    res = run(bundle, TASK_CONSOLE_SKILLS=str(full))
    assert row(res, "skills")["state"] == "ok", row(res, "skills")


def test_an_unlistable_directory_says_so_separately(bundle, tmp_path, monkeypatch):
    """「列不出来」和「真的是空的」要分开说:前者要有人立刻去看,后者不一定。"""
    d = tmp_path / "boom"
    d.mkdir()
    real = os.listdir
    def boom(p):
        if str(p) == str(d):
            raise OSError("nope")
        return real(p)
    monkeypatch.setattr(os, "listdir", boom)
    r = row(run(bundle, TASK_CONSOLE_SKILLS=str(d)), "skills")
    assert r["state"] == "missing"
    assert "列不出来" in (r.get("why") or ""), r
