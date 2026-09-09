#!/usr/bin/env python3
"""仓库扫描的判据测试。

用真的 git 仓跑,不是拿假字符串喂解析器。理由:这个模块的输入是 `git status --porcelain=v2`
的真实输出,而一个自己造输入的测试会在格式变化时继续全绿,同时生产里已经解析不出东西了。

最重要的两条:
  没有 upstream 时 ahead 必须是 None 而不是 0(否则「从没推过」和「已同步」长一样)
  扫不动的仓必须出现在列表里并且是红的(悄悄跳过等于算它没问题)
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import repos as R  # noqa: E402

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="没有 git")

NOW = 1_800_000_000.0


def git(d, *a):
    subprocess.run(("git", "-C", str(d)) + a, capture_output=True, check=True)


def mkbare(base, name):
    """真的 bare 仓。先建工作树再设 core.bare 是推不动的,push 会直接失败。"""
    d = base / name
    d.mkdir(parents=True)
    subprocess.run(("git", "init", "--bare", "-q", str(d)), capture_output=True, check=True)
    return d


def mkrepo(base, name, commit=True):
    d = base / name
    d.mkdir(parents=True)
    git(d.parent, "init", "-q", name)
    git(d, "config", "user.email", "user1@example.com")
    git(d, "config", "user.name", "Example User")
    # 把钩子关掉。开发机上很可能配了全局 core.hooksPath,不关的话每一个测试提交都会
    # 去跑一整套本地钩子:测试慢一个数量级,而且结果开始取决于跑它的那台机器怎么配的。
    git(d, "config", "core.hooksPath", str(base / "_nohooks"))
    if commit:
        (d / "a.txt").write_text("hello\n", encoding="utf-8")
        git(d, "add", "a.txt")
        git(d, "commit", "-q", "-m", "init")
    return d


# ---------- 未配置与空 ----------

def test_unset_root_reports_not_checked(monkeypatch):
    monkeypatch.delenv("TASK_CONSOLE_REPOS", raising=False)
    r = R.scan()
    assert r["available"] is False and r["reason"]


def test_absent_root_reports_not_checked(tmp_path):
    r = R.scan(root=str(tmp_path / "nope"))
    assert r["available"] is False


def test_root_with_no_repos_is_available_but_empty(tmp_path):
    # 「这里没有仓」和「我没去看」必须是两个不同的答案。
    (tmp_path / "notarepo").mkdir()
    r = R.scan(root=str(tmp_path), now=NOW)
    assert r["available"] is True
    assert r["repos"] == [] and r["note"]


# ---------- 四态 ----------

def test_clean_repo_is_clean(tmp_path):
    mkrepo(tmp_path, "a")
    r = R.scan(root=str(tmp_path), now=NOW)["repos"][0]
    assert r["state"] == R.CLEAN
    assert r["dirty"] == 0
    assert r["branch"]


def test_uncommitted_change_is_dirty(tmp_path):
    d = mkrepo(tmp_path, "a")
    (d / "b.txt").write_text("x", encoding="utf-8")
    r = R.scan(root=str(tmp_path), now=NOW)["repos"][0]
    assert r["state"] == R.DIRTY
    assert r["dirty"] >= 1


def test_no_upstream_means_ahead_is_unknown_not_zero(tmp_path):
    # 一个从没设过上游的分支,提交一个都没推出去,而 0/0 会把它显示成「已同步」。
    mkrepo(tmp_path, "a")
    r = R.scan(root=str(tmp_path), now=NOW)["repos"][0]
    assert r["ahead"] is None
    assert r["unpushedKnown"] is False
    assert r["upstream"] is None


def test_ahead_of_upstream_is_unpushed(tmp_path):
    up = mkbare(tmp_path / "bare", "up")
    d = mkrepo(tmp_path, "a")
    git(d, "remote", "add", "origin", str(up))
    git(d, "push", "-q", "-u", "origin", "HEAD")
    (d / "c.txt").write_text("x", encoding="utf-8")
    git(d, "add", "c.txt")
    git(d, "commit", "-q", "-m", "second")
    r = [x for x in R.scan(root=str(tmp_path), now=NOW)["repos"] if x["name"] == "a"][0]
    assert r["state"] == R.UNPUSHED
    assert r["ahead"] == 1
    assert r["unpushedKnown"] is True


def test_synced_repo_is_clean_and_known(tmp_path):
    # 上一条的正对照:设了上游且已同步,必须是 clean 且 ahead==0,不是 None。
    up = mkbare(tmp_path / "bare", "up")
    d = mkrepo(tmp_path, "a")
    git(d, "remote", "add", "origin", str(up))
    git(d, "push", "-q", "-u", "origin", "HEAD")
    r = [x for x in R.scan(root=str(tmp_path), now=NOW)["repos"] if x["name"] == "a"][0]
    assert r["state"] == R.CLEAN
    assert r["ahead"] == 0 and r["unpushedKnown"] is True


def test_unreadable_repo_is_listed_as_error_not_skipped(tmp_path):
    d = mkrepo(tmp_path, "broken")
    # 把 .git 弄坏:git status 会失败,而这个仓必须还在列表里并且是红的。
    for f in (d / ".git").iterdir():
        if f.name == "HEAD":
            f.write_text("garbage\n", encoding="utf-8")
    out = R.scan(root=str(tmp_path), now=NOW)
    names = {x["name"]: x for x in out["repos"]}
    assert "broken" in names, "扫不动的仓被悄悄跳过了"
    assert names["broken"]["state"] == R.ERROR
    assert names["broken"]["why"]


# ---------- 汇总 ----------

def test_attention_counts_the_ones_a_human_must_act_on(tmp_path):
    mkrepo(tmp_path, "clean1")
    d = mkrepo(tmp_path, "dirty1")
    (d / "x").write_text("x", encoding="utf-8")
    s = R.scan(root=str(tmp_path), now=NOW)["summary"]
    assert s["total"] == 2
    assert s["attention"] == 1
    # 没上游的仓数要单独报:它们的「没推」是看不见的。
    assert s["unknownUpstream"] == 2


def test_unpushed_sorts_above_dirty(tmp_path):
    up = mkbare(tmp_path / "bare", "up")
    a = mkrepo(tmp_path, "aaa_unpushed")
    git(a, "remote", "add", "origin", str(up))
    git(a, "push", "-q", "-u", "origin", "HEAD")
    (a / "c").write_text("x", encoding="utf-8")
    git(a, "add", "c")
    git(a, "commit", "-q", "-m", "s")
    b = mkrepo(tmp_path, "zzz_dirty")
    (b / "x").write_text("x", encoding="utf-8")
    order = [x["name"] for x in R.scan(root=str(tmp_path), now=NOW)["repos"]]
    # 脏文件你自己知道,没推的提交没人会告诉你,所以它排在前面。
    assert order.index("aaa_unpushed") < order.index("zzz_dirty")


def test_nested_marker_distinguishes_submodules(tmp_path):
    d = mkrepo(tmp_path, "a")
    r = R.scan(root=str(tmp_path), now=NOW)["repos"][0]
    assert r["nested"] is False
    # submodule 的 .git 是文件不是目录,这个区分是承重的。
    assert (d / ".git").is_dir()


# ---------- fetch 的参数闸 ----------

def test_fetch_refuses_unsafe_names(tmp_path, monkeypatch):
    from maint import Refused
    monkeypatch.setenv("TASK_CONSOLE_REPOS", str(tmp_path))
    for bad in ("../x", "a/b", "", "x;y"):
        with pytest.raises(Refused) as e:
            R.fetch(bad)
        assert e.value.code in ("bad_name", "not_child")


def test_fetch_refuses_a_non_repo(tmp_path, monkeypatch):
    from maint import Refused
    monkeypatch.setenv("TASK_CONSOLE_REPOS", str(tmp_path))
    (tmp_path / "plain").mkdir()
    with pytest.raises(Refused) as e:
        R.fetch("plain")
    assert e.value.code == "missing_src"


# ---------- 可见性表解析失败不能被吞成空表 ----------
# 吞掉之后所有仓的 PUB/PRI 标记一起消失,而那和「表里没登记这几个仓」长得一模一样。
# 在这套体系里可见性正是判断一个仓能不能装真实数据的依据,
# 一个静默变空的可见性视图,比没有这个视图更危险。
# 自检只能证明这个文件存在且非空,证明不了它解析得出来。

def test_a_broken_visibility_table_reports_why(tmp_path, monkeypatch):
    p = tmp_path / "vis.json"
    p.write_text("{", encoding="utf-8")          # 非零字节,所以自检会判它 ok
    monkeypatch.setenv("TASK_CONSOLE_VISIBILITY", str(p))
    table, why = R._load_visibility()
    assert table == {}
    assert why and "解析失败" in why, why


def test_an_unreadable_visibility_table_reports_why(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_VISIBILITY", str(tmp_path / "nope.json"))
    table, why = R._load_visibility()
    assert table == {} and why and "读不到" in why, why


def test_an_absent_setting_is_not_a_failure(monkeypatch):
    """正对照:没配 = 没启用,不是故障。
    分不开的话这条提示会在没配置的机器上天天亮,而天天亮的提示等于没有。"""
    monkeypatch.delenv("TASK_CONSOLE_VISIBILITY", raising=False)
    assert R._load_visibility() == ({}, None)


def test_a_good_table_parses_with_no_reason(tmp_path, monkeypatch):
    p = tmp_path / "vis.json"
    p.write_text('{"a": "PUBLIC"}', encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_VISIBILITY", str(p))
    table, why = R._load_visibility()
    assert table == {"a": "PUBLIC"} and why is None
