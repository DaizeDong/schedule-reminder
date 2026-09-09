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


# ---------- junction 部署的 skill 也要能归档 ----------
# _child 原来解析**完整目标**再取父目录,而本机 skill 是 junction 部署的
# (skills/<name> 指向别处的仓库),于是解析后的父目录在 junction 那一边,和 root 对不上。
# 后果:每个 linked 的条目都挂着一个点了必然失败的归档按钮,而失败信息是一句听起来像
# 路径穿越攻击的「目标不是配置根目录的直接子项」,把排查方向整个带偏。

def _make_link(link, real):
    """建一个目录链接。先试 junction(Windows 上不需要管理员,而本机 skill 用的就是它),
    再退回符号链接。两条都不成才跳过 : 但跳过要说清楚跳的是哪一条,
    因为这条用例测的正是这次要修的那个情形,静默跳过等于没测。"""
    import subprocess as sp
    if os.name == "nt":
        r = sp.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                   capture_output=True, text=True)
        if r.returncode == 0:
            return "junction"
    try:
        link.symlink_to(real, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        return None


def test_a_junctioned_child_is_still_a_child(tmp_path):
    root = tmp_path / "skills"; root.mkdir()
    real = tmp_path / "elsewhere" / "myskill"; real.mkdir(parents=True)
    link = root / "myskill"
    kind = _make_link(link, real)
    if not kind:
        pytest.skip("junction 和 symlink 都建不了,这条用例没能测到 linked 那一路")
    assert os.path.realpath(link) != str(link), "链接没真的建成,这条用例会因为错误的理由通过"
    got = M._child(root, "myskill")
    assert got == link


def test_a_plain_child_still_resolves(tmp_path):
    """正对照:普通目录不能因为放宽了链接就跟着失效。"""
    root = tmp_path / "skills"; root.mkdir()
    (root / "plain").mkdir()
    assert M._child(root, "plain") == root / "plain"


def test_dotdot_is_still_refused_even_if_the_name_gate_is_bypassed(tmp_path, monkeypatch):
    """纵深防御不能因为这次放宽而丢掉。

    这个洞(Path(root/'..').parent 就是 root 本身,于是比对通过)是测试在干净版本上
    抓出来的,改用字面判定之后必须仍然挡得住。SAFE_NAME 平时就会拦下 '..',
    所以要绕过它才能测到第二层。"""
    import re
    monkeypatch.setattr(M, "SAFE_NAME", re.compile(r"^.*$"))
    root = tmp_path / "skills"; root.mkdir()
    with pytest.raises(M.Refused) as e:
        M._child(root, "..")
    assert e.value.code == "not_child", e.value.code


# ---------- 插件清单解析不出来时不许装作一切正常 ----------

class _R:
    def __init__(self, out): self.returncode, self.stdout, self.stderr = 0, out, ""


def test_unparseable_plugin_output_is_not_available(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "_claude", lambda: "claude")
    monkeypatch.setattr(M.subprocess, "run",
                        lambda *a, **k: _R("- my-plugin@1 (enabled)\n- other@2 (enabled)\n"))
    got = M.read_plugins()
    assert got["available"] is False, got
    assert "格式" in got["reason"], got["reason"]


def test_names_without_status_are_not_reported_as_disabled(monkeypatch):
    """认出了名字但没认出状态时,不能把每个都画成「已禁用」并邀请人去启用。"""
    monkeypatch.setattr(M, "_claude", lambda: "claude")
    monkeypatch.setattr(M.subprocess, "run", lambda *a, **k: _R("❯ alpha\n❯ beta\n"))
    got = M.read_plugins()
    assert got["available"] is False, got
    assert "启用状态" in got["reason"], got["reason"]


def test_a_well_formed_listing_is_still_available(monkeypatch):
    """正对照:格式没变时必须照常可用。
    没有这一条,把 read_plugins 改成「永远 available False」也能让上面两条通过。"""
    monkeypatch.setattr(M, "_claude", lambda: "claude")
    monkeypatch.setattr(M.subprocess, "run",
                        lambda *a, **k: _R("❯ alpha\n  Status: enabled\n❯ beta\n  Status: disabled\n"))
    got = M.read_plugins()
    assert got["available"] is True, got
    assert [p["enabled"] for p in got["plugins"]] == [True, False]


# ---------- 读不出来的 skill 描述不能按 0 计入预算 ----------
# 预算条是用来防「超了之后尾部条目的描述会在下一次会话里静默消失」的护栏。
# _desc_len 返回 None 表示「读不出来」,返回 0 表示「真的没写描述」,
# 而 `or 0` 把这两件事压成同一个数:那份 skill 贡献的字符被当成 0,
# 预算条读数偏低、颜色偏绿,而真实预算已经更接近上限。
# 一个被喂了空的度量打印的绿色,和一个真没超标的度量打印的绿色一模一样。

def _skill(root, name, front):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(front, encoding="utf-8")
    return d


def test_an_unreadable_description_is_counted_separately(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "good", "---\ndescription: hello there\n---\nbody\n")
    _skill(root, "broken", "no frontmatter at all\n")     # _desc_len -> None
    monkeypatch.setenv("TASK_CONSOLE_SKILLS", str(root))
    monkeypatch.delenv("TASK_CONSOLE_SKILL_ARCHIVE", raising=False)
    got = M.read_skills()
    assert got["descUnreadable"] == 1, got
    broken = next(s for s in got["skills"] if s["name"] == "broken")
    assert broken["descUnreadable"] is True, broken


def test_a_skill_with_an_empty_description_is_not_called_unreadable(tmp_path, monkeypatch):
    """正对照:真的没写描述(frontmatter 在、没有 description 行)是 0 不是 None。
    分不开的话这个提示会在每台机器上天天亮,而天天亮的提示等于没有。"""
    root = tmp_path / "skills"
    _skill(root, "nodesc", "---\nname: nodesc\n---\nbody\n")
    monkeypatch.setenv("TASK_CONSOLE_SKILLS", str(root))
    monkeypatch.delenv("TASK_CONSOLE_SKILL_ARCHIVE", raising=False)
    got = M.read_skills()
    assert got["descUnreadable"] == 0, got
    assert got["skills"][0]["descUnreadable"] is False


def test_the_memory_hard_limits_have_exactly_one_definition():
    """MEMORY.md 的两条硬上限只能有一份定义。

    ⚠ 它们以前在 maint 和 memops 里**各存了一份**,而两个模块都把它们下发给页面。
    两份手写的同一个数,没有任何东西对账 —— 改一处而另一处照旧,页面上就会出现
    两个都自称权威的百分比,而它们的分母不同。
    这条按**对象同一性**判,不是按值相等:两份恰好写着同一个数字时值也相等,
    而那正是要防的状态。
    """
    import memops as MO
    assert M.INDEX_HARD_LINES is MO.INDEX_HARD_LINES
    assert M.INDEX_HARD_BYTES is MO.INDEX_HARD_BYTES

    # 源码里也不许再出现第二处字面量定义。
    import re
    src = open(M.__file__, encoding="utf-8").read()
    assert not re.search(r"^INDEX_HARD_(LINES|BYTES)\s*=\s*\d", src, re.M), \
        "maint.py 里又出现了硬上限的字面量定义"


def test_the_second_memory_reading_says_it_is_not_authoritative(tmp_path, monkeypatch):
    """/api/maint 里那份记忆池计数必须标明自己不是权威口径。

    记忆池那一屏读的是 /api/mem(memops),而这份算的是同一批数字的另一份,
    口径还不完全一样。**一个不标注口径的第二份数字,和一个错的数字在读的人那里
    代价一样:他得先花时间弄明白该信哪个。**
    """
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(tmp_path))
    r = M.read_memory()
    assert r["available"] is True
    assert r.get("authority") == "/api/mem", "没有标明权威口径在哪"
    assert r.get("note"), "没有说明两边算法不同"
