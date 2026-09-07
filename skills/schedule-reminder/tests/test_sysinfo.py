#!/usr/bin/env python3
"""磁盘残留清理的闸门测试。

这个模块会 rmtree,所以测试的重点全在「它不删什么」上:名字不匹配的、太新的、
不在配置根下的、根本没配的。删除是不可逆的,所以每一条拒绝都要有单独的用例,
而不是靠一句「应该没问题」。

体积统计那部分测的是诚实:数到上限就必须说 partial,不许报一个数了一半的确定数字。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import sysinfo as S  # noqa: E402
from maint import Refused  # noqa: E402

NOW = 1_800_000_000.0
H = 3600.0


def mk(root, name, age_h=99.0, files=1):
    d = root / name
    d.mkdir(parents=True)
    for i in range(files):
        (d / f"f{i}").write_text("x" * 10, encoding="utf-8")
    t = NOW - age_h * H
    os.utime(d, (t, t))
    return d


# ---------- 只删该删的 ----------

def test_removes_only_old_temp_git_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_PLUGIN_CACHE", str(tmp_path))
    mk(tmp_path, "temp_git_1788228267493_f9m8wk", age_h=50)
    mk(tmp_path, "temp_git_1788228267516_8nv83j", age_h=50)
    keep = mk(tmp_path, "claude-plugins-official", age_h=50)
    r = S.clean_temp_git(now=NOW)
    assert r["removed"] == 2
    assert keep.is_dir(), "把不匹配名字的真实目录删掉了"


def test_recent_leftovers_are_skipped(tmp_path, monkeypatch):
    # 一个正在进行中的克隆看起来和一个废弃的一模一样,唯一的区别是年龄。
    monkeypatch.setenv("TASK_CONSOLE_PLUGIN_CACHE", str(tmp_path))
    d = mk(tmp_path, "temp_git_1788228267493_f9m8wk", age_h=0.5)
    r = S.clean_temp_git(now=NOW)
    assert r["removed"] == 0 and r["skipped"] == 1
    assert d.is_dir()


@pytest.mark.parametrize("name", [
    "temp_git", "temp_git_", "temp_gitXX_1_a", "tempgit_1_a",
    "temp_git_abc_x", "atemp_git_1_a", "temp_git_1_a_b_c/x",
])
def test_names_that_must_not_match(name):
    assert not S.TEMP_GIT.match(name), name


def test_the_real_shape_does_match():
    # 正对照。一个什么都不匹配的正则会让上面那一整排全绿。
    assert S.TEMP_GIT.match("temp_git_1788228267493_f9m8wk")


def test_unconfigured_cache_refuses_to_delete(monkeypatch):
    monkeypatch.delenv("TASK_CONSOLE_PLUGIN_CACHE", raising=False)
    with pytest.raises(Refused) as e:
        S.clean_temp_git(now=NOW)
    assert e.value.code == "no_config"


def test_absent_cache_dir_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_PLUGIN_CACHE", str(tmp_path / "nope"))
    with pytest.raises(Refused) as e:
        S.clean_temp_git(now=NOW)
    assert e.value.code == "missing_src"


def test_freed_bytes_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_PLUGIN_CACHE", str(tmp_path))
    mk(tmp_path, "temp_git_1_aa", age_h=50, files=5)
    r = S.clean_temp_git(now=NOW)
    assert r["removed"] == 1 and r["freedBytes"] >= 50


# ---------- 读取契约 ----------

def test_unset_sources_report_not_checked(monkeypatch):
    monkeypatch.delenv("TASK_CONSOLE_PLUGIN_CACHE", raising=False)
    monkeypatch.delenv("TASK_CONSOLE_SESSIONS", raising=False)
    r = S.read(now=NOW)
    assert r["pluginCache"]["available"] is False and r["pluginCache"]["reason"]
    assert r["sessions"]["available"] is False and r["sessions"]["reason"]


def test_leftovers_are_listed_with_age_and_deletability(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_PLUGIN_CACHE", str(tmp_path))
    mk(tmp_path, "temp_git_1_aa", age_h=50)
    mk(tmp_path, "temp_git_2_bb", age_h=0.2)
    pc = S.read(now=NOW)["pluginCache"]
    assert pc["leftoverCount"] == 2
    assert pc["deletableCount"] == 1     # 太新的那个不算可删


def test_dir_size_says_partial_when_it_stops_early(tmp_path):
    for i in range(12):
        (tmp_path / f"f{i}").write_text("x", encoding="utf-8")
    full = S.dir_size(tmp_path)
    assert full["partial"] is False and full["files"] == 12
    # 数到上限就必须说 partial。一个数了一半却报出确定数字的体积比不报还糟。
    capped = S.dir_size(tmp_path, cap=5)
    assert capped["partial"] is True
    assert capped["files"] <= 12


def test_disk_reports_free_space():
    d = S.disk(".")
    assert d["available"] is True
    assert d["free"] > 0 and 0 <= d["usedPct"] <= 100


def test_readonly_files_do_not_block_removal(tmp_path, monkeypatch):
    """只读文件不能让删除静默变成「跳过」。

    git 把 objects/ 下的文件建成只读,Windows 上删一个只读文件直接 PermissionError。
    实测:第一次真跑时 42 个废弃目录有一半卡在这里,而它们在结果里只是「跳过 21」,
    没有任何东西说明原因 : 一个说不出自己为什么没干成的清理器,和一个真的没什么可清的
    清理器,输出长得一模一样。
    """
    monkeypatch.setenv("TASK_CONSOLE_PLUGIN_CACHE", str(tmp_path))
    d = mk(tmp_path, "temp_git_9_ro", age_h=50)
    f = d / "locked"
    f.write_text("x", encoding="utf-8")
    os.chmod(f, 0o444)
    t = NOW - 50 * H
    os.utime(d, (t, t))
    r = S.clean_temp_git(now=NOW)
    assert r["removed"] == 1, f"只读文件把删除挡住了: skipped={r['skipped']}"
    assert not d.exists()
