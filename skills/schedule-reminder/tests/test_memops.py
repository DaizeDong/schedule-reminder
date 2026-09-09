#!/usr/bin/env python3
"""记忆池诊断的测试。

三项诊断各自都可能是空的,而空在这里是好消息。所以每一项都配一对用例:
一条造出问题断言它被抓到,一条造一个干净的池子断言它确实报空。
只有前者会让一个「永远返回空列表」的实现全绿。

归档动作不在这里实现,所以测的是**它有没有正确地把调用转出去**,以及参数闸。
真归档器不参与测试:测试自己写一个假的,断言拿到的是预期的参数。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import memops as M  # noqa: E402
from maint import Refused  # noqa: E402


def pool(tmp_path, entries=(), cold=(), index_lines=None, links=None):
    root = tmp_path / "memory"
    (root / "archive").mkdir(parents=True)
    for slug in entries:
        (root / f"{slug}.md").write_text(
            f"---\nname: {slug}\n---\n{(links or {}).get(slug, '')}\n", encoding="utf-8")
    for slug in cold:
        (root / "archive" / f"{slug}.md").write_text(
            f"---\nname: {slug}\n---\n", encoding="utf-8")
    idx = index_lines if index_lines is not None else [
        f"- [{s}]({s}.md) : hook" for s in entries]
    (root / "MEMORY.md").write_text("\n".join(idx) + "\n", encoding="utf-8")
    (root / "archive" / "MEMORY-archive.md").write_text(
        "\n".join(f"- [{s}]({s}.md) : hook" for s in cold) + "\n", encoding="utf-8")
    return root


# ---------- 未配置 ----------

def test_unset_root_is_not_checked(monkeypatch):
    monkeypatch.delenv("TASK_CONSOLE_MEMORY", raising=False)
    r = M.read()
    assert r["available"] is False and r["reason"]


# ---------- 孤儿链接 ----------

def test_orphan_link_is_reported(tmp_path, monkeypatch):
    root = pool(tmp_path, entries=("a", "b"), links={"a": "see [[nowhere]] and [[b]]"})
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    r = M.read()
    names = [o["name"] for o in r["orphanLinks"]]
    assert names == ["nowhere"]
    assert r["orphanLinks"][0]["from"] == ["a"]


def test_a_clean_pool_reports_no_orphans(tmp_path, monkeypatch):
    # 正对照。没有它,一个永远返回空列表的实现在上面那条之外全绿。
    root = pool(tmp_path, entries=("a", "b"), links={"a": "see [[b]]"})
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    assert M.read()["orphanLinks"] == []


def test_a_link_into_the_cold_layer_is_not_an_orphan(tmp_path, monkeypatch):
    # 归档过的记忆仍然可达,指向它的链接不是断的。
    root = pool(tmp_path, entries=("a",), cold=("old",), links={"a": "[[old]]"})
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    assert M.read()["orphanLinks"] == []


# ---------- 可达性 ----------

def test_file_missing_from_the_index_is_unreachable(tmp_path, monkeypatch):
    root = pool(tmp_path, entries=("a", "b"), index_lines=["- [a](a.md) : hook"])
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    r = M.read()
    # 池子里有 b 但没有任何索引行指向它:打开索引的人永远不会知道它存在。
    assert r["unreachable"] == ["b"]


def test_index_line_pointing_at_nothing_is_dangling(tmp_path, monkeypatch):
    root = pool(tmp_path, entries=("a",),
                index_lines=["- [a](a.md) : hook", "- [ghost](ghost.md) : hook"])
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    assert M.read()["danglingIndex"] == ["ghost"]


def test_fully_consistent_pool_reports_both_empty(tmp_path, monkeypatch):
    root = pool(tmp_path, entries=("a", "b"))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    r = M.read()
    assert r["unreachable"] == [] and r["danglingIndex"] == []


# ---------- 硬上限 ----------

def test_index_is_measured_against_the_hard_limits(tmp_path, monkeypatch):
    root = pool(tmp_path, entries=tuple(f"s{i}" for i in range(5)))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    r = M.read()
    assert r["hardLines"] == 200 and r["hardBytes"] == 25600
    assert 0 < r["linePct"] <= 100
    # 上限不是建议:超了尾部条目会在下一次会话里静默消失。
    assert r["indexLines"] and r["indexBytes"]


def test_biggest_entries_are_listed_largest_first(tmp_path, monkeypatch):
    root = pool(tmp_path, entries=("small", "big"))
    # 保留 frontmatter:覆盖掉它会让这个文件不再算记忆,于是它根本不进列表。
    (root / "big.md").write_text("---" + chr(10) + "name: big" + chr(10) + "---" + chr(10) + "x" * 5000, encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    assert M.read()["biggest"][0]["slug"] == "big"


# ---------- 动作只是转发 ----------

def fake_archiver(tmp_path, rc=0):
    p = tmp_path / "fake_archiver.py"
    p.write_text(
        "import sys\n"
        "print('ARGS ' + ' '.join(sys.argv[1:]))\n"
        f"sys.exit({rc})\n", encoding="utf-8")
    return p


def test_archive_forwards_explicit_slug_to_the_existing_archiver(tmp_path, monkeypatch):
    root = pool(tmp_path, entries=("a",))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY_ARCHIVER", str(fake_archiver(tmp_path)))
    out = M.act("archive", "a")["out"]
    assert "--archive a" in out
    # 归档器要求显式 slug,不能是通配或空:转发时必须原样带上。
    assert "--memory-dir" in out


def test_restore_is_the_other_allowed_verb(tmp_path, monkeypatch):
    root = pool(tmp_path, entries=("a",))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY_ARCHIVER", str(fake_archiver(tmp_path)))
    assert "--restore a" in M.act("restore", "a")["out"]


@pytest.mark.parametrize("verb", ["delete", "reindex", "list", "", "archive;rm"])
def test_other_verbs_are_refused(tmp_path, monkeypatch, verb):
    root = pool(tmp_path, entries=("a",))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY_ARCHIVER", str(fake_archiver(tmp_path)))
    with pytest.raises(Refused) as e:
        M.act(verb, "a")
    assert e.value.code == "bad_action"


@pytest.mark.parametrize("slug", ["../x", "a/b", "", "a b", "a;b"])
def test_unsafe_slugs_are_refused(tmp_path, monkeypatch, slug):
    root = pool(tmp_path, entries=("a",))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY_ARCHIVER", str(fake_archiver(tmp_path)))
    with pytest.raises(Refused) as e:
        M.act("archive", slug)
    assert e.value.code == "bad_name"


def test_archiver_failure_surfaces_instead_of_looking_like_success(tmp_path, monkeypatch):
    root = pool(tmp_path, entries=("a",))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY_ARCHIVER", str(fake_archiver(tmp_path, rc=3)))
    with pytest.raises(Refused) as e:
        M.act("archive", "a")
    assert e.value.code == "archiver_failed"


def test_unconfigured_archiver_refuses(tmp_path, monkeypatch):
    root = pool(tmp_path, entries=("a",))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    monkeypatch.delenv("TASK_CONSOLE_MEMORY_ARCHIVER", raising=False)
    with pytest.raises(Refused) as e:
        M.act("archive", "a")
    assert e.value.code == "no_config"


def test_documents_without_frontmatter_are_not_memories(tmp_path, monkeypatch):
    """池子里的文档不是记忆,不能被算成不可达,也不能贡献孤儿链接。

    实测:记忆池宪法没有 frontmatter,而且正文里写着占位符形式的双括号链接。
    把它当记忆扫,每次都会凭空报出一个不可达文件加一个孤儿链接,而一个天天亮着的
    假告警等于没有告警。

    判据是形状不是名字:按名字排除的话,每加一份新文档都要有人记得回来改那张表,
    而忘了改的表现正是这条假告警本身。
    """
    root = pool(tmp_path, entries=("a",))
    (root / "README.md").write_text(
        "# 宪法\n\n用双括号占位符 [[slug]] 表示一个名字。\n", encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    r = M.read()
    assert r["unreachable"] == []
    assert r["orphanLinks"] == []
    assert r["live"] == 1


def test_a_real_memory_is_still_counted(tmp_path, monkeypatch):
    # 上一条的正对照:形状判据不能严到把真记忆也排除掉。
    root = pool(tmp_path, entries=("a", "b"))
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(root))
    assert M.read()["live"] == 2


# ---------- 索引读不到 != 索引是空的 ----------
# available 只表示**记忆池目录**读到了。MEMORY.md 不在或读不了时,
# linePct / bytePct / indexLines / indexBytes 全是 None,而 available 仍然是 True。
# 前端那一格原来写的是 `MEM.linePct||0` —— 于是印出**绿色的 0%** 和字面量「null/200 行」:
# 一个查不成的东西被画成了最健康的样子。一个 None 说不出自己为什么是 None,所以要有原因。

def test_a_missing_index_says_why(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(tmp_path))
    r = M.read()
    assert r["available"] is True, "目录是读到了的,available 不该因为索引缺失变假"
    assert r["linePct"] is None and r["bytePct"] is None
    assert r["indexReason"], "索引读不到却没有给出原因"
    assert "MEMORY.md" in r["indexReason"]


def test_a_present_index_gives_no_reason(tmp_path, monkeypatch):
    """负对照:索引在的时候不许报原因。见谁都叫的字段会被无视。"""
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("- [x](a.md)\n" * 10, encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_MEMORY", str(tmp_path))
    r = M.read()
    assert r["indexReason"] is None
    assert r["linePct"] is not None and r["indexLines"] == 11
