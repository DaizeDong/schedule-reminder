#!/usr/bin/env python3
"""两份机器配置的读取:allow-list 与健康清单。

两处缺陷同一个形状 —— **读取器悄悄少读了东西,而下游没有任何一处能看出来。**

1. allow-list(`$TaskNames = @( ... )`)曾经有三份互不相同的解析实现:
   `server` 要求收尾括号顶格,`retire` 允许它缩进,`retire` 的重写还有第三种判据。
   于是脚本按常见 PowerShell 风格把收尾括号写成缩进时:`server` 匹配失败 -> allow=None ->
   整张表的「备份」列显示 ?、「不在备份清单」这条 issue 对所有任务一律不报,
   **一个真的漏了备份的任务被显示成「未检查」**;而同一时刻 `retire` 把同一个文件
   读得好好的,报「这一处要改」并真的改写它。同一个文件,两个都自称权威的答案。

2. 健康清单里同一个任务名可以有多条声明 —— 一个任务把几件事折叠进来之后,每件事各写一条,
   而监控器是逐条评估的。控制台这边是 `{t["name"]: t for ...}`,**只留最后一条**,
   于是它显示的阈值可能比监控器实际执行的更松;漏写 name 的条目则直接消失,
   而「产物新鲜度覆盖率」算的是过滤之后那份,**对条目被吃掉这件事完全免疫**:
   清单写了 12 条丢了 1 条,页面显示「覆盖 100%」,绿色。
"""
import json
import os
import sys

import pytest

_SCR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "task_console")
sys.path.insert(0, _SCR)

import allowlist as AL  # noqa: E402
import retire as R  # noqa: E402
import server as S  # noqa: E402

Q = chr(39)
D = chr(36)


def _block(body_lines, close="  )"):
    return D + "TaskNames = @(\n" + "\n".join(body_lines) + "\n" + close + "\n"


# --------------------------------------------------------------- allow-list
@pytest.mark.parametrize("close", [")", "  )", "\t)"])
def test_the_closing_paren_may_be_indented(close):
    """收尾括号缩进与否都必须读得出来。

    这正是两份实现分叉的那一处:一边顶格才认,一边随便。
    """
    txt = _block([f"  {Q}AcmeAlpha{Q},", f"  {Q}AcmeBeta{Q}"], close)
    names, why = AL.parse_names(txt)
    assert names == {"AcmeAlpha", "AcmeBeta"}, why


def test_a_comment_containing_an_apostrophe_does_not_eat_the_list():
    """整行注释要先去掉再扫引号。

    英文散文里的撇号和一个开引号长得一模一样;另一个仓的闸门就是这样连续九轮绿灯之后
    突然报出三个不存在的漂移,同时掩盖掉一个真的。
    """
    txt = _block([f"  # this is the repo{Q}s own list",
                  f"  {Q}AcmeGamma{Q}"])
    names, why = AL.parse_names(txt)
    assert names == {"AcmeGamma"}, why


def test_two_names_on_one_line_are_both_read():
    txt = _block([f"  {Q}AcmeOne{Q}, {Q}AcmeTwo{Q}"])
    names, _ = AL.parse_names(txt)
    assert names == {"AcmeOne", "AcmeTwo"}


def test_a_missing_block_is_not_an_empty_set():
    """找不到块要返回 None 而不是空集合。

    None 是「我没能检查」,空集合是「一个任务都没有被备份」—— 后者响亮得多。
    折叠它们会让「解析器坏了」长得像「你的备份清单是空的」。
    """
    names, why = AL.parse_names("# 这个文件里根本没有那个块\n")
    assert names is None and why


def test_server_and_retire_read_the_same_file_the_same_way(tmp_path, monkeypatch):
    """两个消费者对同一个文件必须给出同一个答案。

    这一条才是真正的回归:上面几条只测那个共享函数,而缺陷本来就出在
    「有两份实现」。这里让两个模块各自去读同一个文件。
    """
    p = tmp_path / "sync-from-local.ps1"
    p.write_text(_block([f"  {Q}AcmeAlpha{Q},", f"  {Q}AcmeBeta{Q}"], "  )"),
                 encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_ALLOWLIST", str(p))

    names, why = S.load_allowlist()
    assert names == {"AcmeAlpha", "AcmeBeta"}, why

    m = R.TASKNAMES_BLOCK.search(p.read_text(encoding="utf-8"))
    assert m, "retire 那边读不到同一个块"
    assert set(AL._NAME.findall(m.group(2))) == names


# --------------------------------------------------------------- 健康清单
def _manifest(tmp_path, monkeypatch, tasks):
    p = tmp_path / "task-health.json"
    p.write_text(json.dumps({"tasks": tasks}, ensure_ascii=True), encoding="ascii")
    monkeypatch.setenv("TASK_CONSOLE_HEALTH", str(p))
    return p


def test_an_entry_without_a_name_is_reported_not_swallowed(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, [
        {"name": "AcmeAlpha", "max_age_hours": 26},
        {"task": "AcmeTypo", "max_age_hours": 26},      # name 打错成 task
    ])
    health, warn = S.load_health()
    assert set(health) == {"AcmeAlpha"}
    assert warn and "没有 name" in warn, warn
    assert "2 条" in warn and "1 个任务名" in warn, warn


def test_repeated_names_merge_to_the_strictest_declaration(tmp_path, monkeypatch):
    """同名多条要按**最严**的合并,不能只留最后一条。

    只留最后一条时,界面显示的阈值可能比监控器实际执行的更松 ——
    而松的那一侧会让人以为已经查过了。
    """
    _manifest(tmp_path, monkeypatch, [
        {"name": "AcmeFolded", "max_age_hours": 26,
         "artifact": "~/a/CHANGELOG.md", "artifact_max_age_hours": 26},
        {"name": "AcmeFolded", "max_age_hours": 26,
         "artifact": "~/a/journal", "artifact_max_age_hours": 26},
        {"name": "AcmeFolded", "max_age_hours": 26,
         "artifact": "~/a/CHANGELOG.md", "artifact_max_age_hours": 48},
    ])
    health, warn = S.load_health()
    e = health["AcmeFolded"]
    assert e["artifact_max_age_hours"] == 26, "取到了最松的那条(48),而不是最严的 26"
    assert e["declCount"] == 3
    # 另一条声明盯的产物不能从界面上整个消失。
    assert set(e.get("artifacts") or []) == {"~/a/CHANGELOG.md", "~/a/journal"}
    assert warn and "AcmeFolded" in warn, warn


def test_a_clean_manifest_produces_no_warning(tmp_path, monkeypatch):
    """负对照:清单干净时不许报警。见谁都叫的闸门会被无视。"""
    _manifest(tmp_path, monkeypatch, [
        {"name": "AcmeAlpha", "max_age_hours": 26},
        {"name": "AcmeBeta", "max_age_hours": 26},
    ])
    health, warn = S.load_health()
    assert set(health) == {"AcmeAlpha", "AcmeBeta"}
    assert warn is None, warn
    assert "declCount" not in health["AcmeAlpha"], "单条声明不该被打上合并标记"
