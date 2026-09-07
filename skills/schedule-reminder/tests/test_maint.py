#!/usr/bin/env python3
"""maint 的参数闸与读取契约。

这个模块会移动目录,所以下面一半的用例是攻击面:路径穿越、绝对路径、空名、
指向别处的链接、不在表里的动作。每一条都必须**拒绝整个动作**,而不是清洗一下再执行。

另一半是「未配置」契约:没配根目录时必须报 available=False 并说明原因,
而不是返回一个空列表:空列表在页面上和「一个都没有问题」长得一模一样。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import maint as M  # noqa: E402


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    live = tmp_path / "skills"
    arch = tmp_path / "archive"
    live.mkdir()
    for n in ("alpha", "beta"):
        d = live / n
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: %s\ndescription: 一句话\n---\n正文\n" % n, encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_SKILLS", str(live))
    monkeypatch.setenv("TASK_CONSOLE_SKILL_ARCHIVE", str(arch))
    return live, arch


# ---------- 名字闸 ----------

@pytest.mark.parametrize("bad", [
    "../escape", "..", ".", "a/b", "a\\b", "", "  ", ".hidden",
    "C:/Windows", "/etc/passwd", "a" * 200, "na;me", "na me", "na&me",
])
def test_unsafe_names_are_refused(dirs, bad):
    # 断言的是**哪一道闸**挡下来的,不是「有没有抛异常」。多道闸互相兜底时,
    # 只断言抛异常的用例在投毒下会照样全绿:实测放开名字闸之后子项闸仍然挡住,
    # 于是这一整排在「名字闸形同虚设」的版本上全部通过。
    with pytest.raises(M.Refused) as e:
        M.act("skill.archive", bad)
    assert e.value.code in ("bad_name", "not_child")


def test_a_safe_name_is_not_refused_by_the_name_gate(dirs):
    # 正对照。没有它,一个把所有名字都拒掉的闸门会让上面那一整排全绿。
    live, arch = dirs
    M.act("skill.archive", "alpha")
    assert (arch / "alpha" / "SKILL.md").is_file()
    assert not (live / "alpha").exists()


def test_action_not_in_the_table_is_refused(dirs):
    # 同理。去掉动作表之后 "skill.delete" 会退化成 restore,因源目录不存在照样抛
    # Refused,于是一个只断言「抛了」的用例在投毒下全绿而闸门其实已经没了。
    for a in ("skill.delete", "plugin.install", "rm", "skill.archive; rm -rf /"):
        with pytest.raises(M.Refused) as e:
            M.act(a, "alpha")
        assert e.value.code == "bad_action", a


def test_action_table_is_exactly_these_nine(dirs):
    """钉住集合本身:加动作是一个要有人明确改这行的动作,不是顺手就能滑进去的。

    repo.fetch 在这里,而 repo.push **不在**,这是刻意的:push 是对外动作,撤不回来,
    一个能一键推送的按钮迟早会在没人看的时候被点到。
    """
    assert set(M.ACTIONS) == {"skill.archive", "skill.restore",
                              "plugin.enable", "plugin.disable", "repo.fetch",
                              "clean.tempgit", "memory.archive", "memory.restore",
                              "task.retire"}
    assert not any(a.endswith(".push") for a in M.ACTIONS)
    # 只有一个删除动作,而且它不收路径参数:删哪些由模块自己按形状加年龄判定。
    assert [a for a in M.ACTIONS if a.startswith("clean.")] == ["clean.tempgit"]


# ---------- 移动语义 ----------

def test_round_trip_restores_the_skill(dirs):
    live, arch = dirs
    M.act("skill.archive", "beta")
    assert not (live / "beta").exists()
    M.act("skill.restore", "beta")
    assert (live / "beta" / "SKILL.md").is_file()
    assert not (arch / "beta").exists()


def test_missing_source_is_refused_not_silently_ok(dirs):
    with pytest.raises(M.Refused) as e:
        M.act("skill.archive", "nosuch")
    assert e.value.code == "missing_src"


def test_existing_target_is_never_overwritten(dirs):
    live, arch = dirs
    arch.mkdir(parents=True, exist_ok=True)
    (arch / "alpha").mkdir()
    (arch / "alpha" / "SKILL.md").write_text("旧的\n", encoding="utf-8")
    with pytest.raises(M.Refused) as e:
        M.act("skill.archive", "alpha")
    assert e.value.code == "dst_exists"
    # 旧的那份必须原样还在:一个会覆盖的归档动作是个删除动作。
    assert (arch / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "旧的\n"


def test_unconfigured_archive_refuses_to_move_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_SKILLS", str(tmp_path))
    monkeypatch.delenv("TASK_CONSOLE_SKILL_ARCHIVE", raising=False)
    with pytest.raises(M.Refused) as e:
        M.act("skill.archive", "alpha")
    assert e.value.code == "no_config"


def test_child_gate_holds_when_the_name_gate_is_bypassed(tmp_path, monkeypatch):
    """第二道防线单独验一次,方法是在测试里把第一道拆掉。

    这条用例存在的理由是一次投毒失败:去掉子项闸之后整套测试照样全绿。原因不是测试写坏了,
    是**名字闸完好时子项闸根本够不着**:名字里不允许任何分隔符,于是 root/name 永远
    是直接子项。也就是说它作为「防名字穿越的第二层」是不可达的,它真正防的是 root
    自己被指到别处。

    所以这里临时把名字闸放开,再喂一个穿越形状,断言子项闸自己会接住。不这么做,
    这段代码就只是一段没人验证过、看起来很安全的字符。
    """
    root = tmp_path / "r"
    root.mkdir()
    monkeypatch.setattr(M, "SAFE_NAME", __import__("re").compile(r".*", __import__("re").S))
    for bad in ("..", "sub/deeper", "../outside"):
        with pytest.raises(M.Refused) as e:
            M._child(root, bad)
        assert e.value.code == "not_child", bad
    # 正对照:一个真正的直接子项必须通过,否则这道闸等于把功能焊死。
    assert M._child(root, "ok").name == "ok"


def test_missing_source_refusal_names_the_source(dirs):
    with pytest.raises(M.Refused) as e:
        M.act("skill.restore", "alpha")
    assert e.value.code == "missing_src"


# ---------- 未配置必须与没问题分开 ----------

def test_unset_skills_root_reports_not_checked(monkeypatch):
    monkeypatch.delenv("TASK_CONSOLE_SKILLS", raising=False)
    r = M.read_skills()
    assert r["available"] is False and r["reason"]


def test_unset_memory_root_reports_not_checked(monkeypatch):
    monkeypatch.delenv("TASK_CONSOLE_MEMORY", raising=False)
    r = M.read_memory()
    assert r["available"] is False and r["reason"]


def test_skills_read_counts_description_budget(dirs, monkeypatch):
    r = M.read_skills()
    assert r["available"] is True
    assert r["liveCount"] == 2
    # 预算是名字加描述,两者都进系统提示,只数其中一个会低估。
    assert r["budgetChars"] > 0
    assert {s["name"] for s in r["skills"]} == {"alpha", "beta"}


def test_archived_skills_are_listed_separately(dirs):
    M.act("skill.archive", "alpha")
    r = M.read_skills()
    assert r["archivedCount"] == 1
    assert [s["archived"] for s in r["skills"] if s["name"] == "alpha"] == [True]
    # 归档掉的不再计入预算:它已经不在系统提示里了。
    assert r["liveCount"] == 1


def test_memory_reports_index_against_the_hard_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(tmp_path))
    (tmp_path / "MEMORY.md").write_text("行\n" * 10, encoding="utf-8")
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    r = M.read_memory()
    assert r["available"] is True
    assert r["indexLines"] and r["indexBytes"]
    # 上限是护栏不是建议:超了尾部条目会在下次会话静默消失。
    assert r["hardLines"] == 200 and r["hardBytes"] == 25600


def test_plugin_read_without_claude_says_not_checked(monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_CLAUDE", "/definitely/not/here")
    monkeypatch.setattr(M.shutil, "which", lambda _n: None)
    r = M.read_plugins()
    assert r["available"] is False and r["reason"]
