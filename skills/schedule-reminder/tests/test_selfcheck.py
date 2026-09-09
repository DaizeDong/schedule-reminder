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


def _env_vars_production_actually_reads():
    """扫生产代码,列出它真的读的每一个 TASK_CONSOLE_* 环境变量。

    **期望值必须从测试之外独立观察出来。** 这条用例原来断言的是
    「一张手写的 8 项名单 ⊆ SC.SOURCES」——单向包含,只能证明「这 8 个还在」,
    永远证明不了「没有第九个被漏掉」;而且对 SOURCES 被删条目完全无感。
    实测:把 SOURCES 里 categories 和 convo_cache 两条真实来源整条删掉,
    这个文件 18 条用例照样全过(其余用例用 len(SC.SOURCES) 算期望值,跟着缩水)。
    而漏登记此刻就有三个。
    """
    import glob
    import re
    scr = os.path.dirname(SC.__file__)
    pat = re.compile(r"TASK_CONSOLE_[A-Z_]+")
    found = {}
    for f in sorted(glob.glob(os.path.join(scr, "*.py"))):
        with open(f, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if line.lstrip().startswith("#"):
                    continue            # 讲这条规则的注释不算「读」
                for m in pat.findall(line):
                    found.setdefault(m, []).append(f"{os.path.basename(f)}:{i}")
    return found


def test_every_env_var_production_reads_is_accounted_for():
    """每一个被读的环境变量,要么是面板来源,要么是**写明了理由的**覆盖项。

    双向对账:多一个少一个都会红。
    """
    found = _env_vars_production_actually_reads()
    assert len(found) >= 10, f"只扫到 {len(found)} 个,扫描器大概没扫到东西"

    sources = {v for _k, _t, v, _kind, _age, _req in SC.SOURCES if v}
    overrides = {v for v, _why in SC.OVERRIDES}
    accounted = sources | overrides

    unaccounted = sorted(set(found) - accounted)
    assert not unaccounted, (
        "这些环境变量生产代码在读,而自检既没把它当来源、也没声明成覆盖项:\n  "
        + "\n  ".join(f"{v}  (首次出现 {found[v][0]})" for v in unaccounted))

    stale = sorted(accounted - set(found))
    assert not stale, (
        "这些登记着、而生产代码里已经没人读了:\n  " + "\n  ".join(stale)
        + "\n(登记一个没人读的来源,会让自检的分母虚高。)")


def test_every_override_states_why_it_is_not_a_panel_source():
    """覆盖项必须写明为什么不算面板来源。

    没有理由的豁免和没有豁免的区别,只是前者看起来像有人想过。
    """
    assert SC.OVERRIDES, "OVERRIDES 是空的,那上面那条对账就没在放行任何东西"
    for var, why in SC.OVERRIDES:
        assert var.startswith("TASK_CONSOLE_"), var
        assert why and len(why) > 20, f"{var} 的豁免理由太短,写不清就不该豁免: {why!r}"


def test_deleting_a_real_source_is_caught():
    """负对照:从 SOURCES 里删掉一条真实来源,对账必须红。

    这正是原来那条用例做不到的事 —— 它只查一张手写名单里的名字在不在,
    删掉名单之外的任何一条它都无感。
    """
    found = _env_vars_production_actually_reads()
    overrides = {v for v, _why in SC.OVERRIDES}
    trimmed = tuple(r for r in SC.SOURCES if r[2] != "TASK_CONSOLE_CATEGORIES")
    assert len(trimmed) == len(SC.SOURCES) - 1, "没删掉,这条负对照没成立"
    accounted = {v for _k, _t, v, _kind, _age, _req in trimmed if v} | overrides
    assert set(found) - accounted, "删掉一条真实来源之后对账居然还是干净的"


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


def test_a_landing_zone_directory_may_legitimately_be_empty(bundle, tmp_path):
    """归档区还没归档过东西时就是空的,那不是故障。

    上线当天实测到:「空目录 = 没配好」这条规则对着一个空的 skill 归档区喊「读不到」,
    而**一道对合法状态开火的闸门会被忽略,连带它真正该抓的那一类一起被忽略**。
    """
    empty = tmp_path / "archive"
    empty.mkdir()
    r = row(run(bundle, TASK_CONSOLE_SKILL_ARCHIVE=str(empty)), "skill_archive")
    assert r["state"] == "ok", r


def test_the_exemption_does_not_leak_to_sources_that_must_have_content(bundle, tmp_path):
    """正对照:豁免只给名单里那几个。skill 目录空了仍然要红,
    否则这次放宽会把它本来该抓的那一类一起放过。"""
    empty = tmp_path / "skills"
    empty.mkdir()
    assert row(run(bundle, TASK_CONSOLE_SKILLS=str(empty)), "skills")["state"] == "missing"


def test_the_exemption_list_is_not_everything(bundle):
    """再一条:名单不能悄悄膨胀成「所有目录都豁免」。"""
    import selfcheck as SC2
    dirs = {k for k, _t, _v, kind, _m, _r in SC2.SOURCES if kind == "dir"}
    assert SC2.MAY_BE_EMPTY < dirs, "豁免名单覆盖了全部目录,等于把这条检查关掉了"
