#!/usr/bin/env python3
"""对话历史扫描的测试。

两条最重要的判据各配一对用例:

**什么算一条真人消息。** 人打的字在转录里是纯字符串,系统注入的东西(skill 正文、
工具结果、提醒块)是内容块。不区分的话,一个计划任务的转录里会有十几条「用户消息」,
因为它加载的每个 skill 正文都算一条,于是每个无头运行都被判成一场多轮对话。
实测按块也算时,一个纯自动化目录报出 247 场「真人对话」,改判据后是 0。

**分组用转录里记的 cwd,不是目录名。** 目录名是把分隔符和点号都换成短横做出来的,
这个变换不可逆,用它分组会把不同项目并到一起而且看不出来。
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import convos as C  # noqa: E402

NOW = 1_800_000_000.0


def line(**kw):
    return json.dumps(kw, ensure_ascii=False) + "\n"


def user_typed(text, cwd="C:/proj", ts="2026-09-07T00:00:00Z"):
    return line(type="user", cwd=cwd, timestamp=ts, message={"content": text})


def user_block(text, cwd="C:/proj"):
    """系统注入的:content 是块列表,不是字符串。"""
    return line(type="user", cwd=cwd, timestamp="2026-09-07T00:00:00Z",
                message={"content": [{"type": "text", "text": text}]})


def session(tmp_path, dirname, fname, body):
    d = tmp_path / dirname
    d.mkdir(parents=True, exist_ok=True)
    f = d / (fname + ".jsonl")
    f.write_text(body, encoding="utf-8")
    return f


# ---------- 未配置 ----------

def test_unset_root_is_not_checked(monkeypatch):
    monkeypatch.delenv("TASK_CONSOLE_SESSIONS", raising=False)
    r = C.scan()
    assert r["available"] is False and r["reason"]


def test_absent_root_is_not_checked(tmp_path):
    r = C.scan(root=str(tmp_path / "nope"))
    assert r["available"] is False


# ---------- 什么算一条真人消息 ----------

def test_typed_strings_count_as_human(tmp_path):
    session(tmp_path, "g1", "a", user_typed("帮我看看这个") + user_typed("再改一下"))
    g = C.scan(root=str(tmp_path), now=NOW)["groups"][0]
    assert g["shown"][0]["humanSeen"] == 2
    assert g["humanish"] == 1


def test_injected_blocks_do_not_count(tmp_path):
    """一个无头运行:一条真提示 + 一堆被注入的 skill 正文。

    它是一次自动化调用,不是一场对话。按块也算的话它会被判成多轮。
    """
    body = user_typed("Run the daily sweep")
    for i in range(12):
        body += user_block("# Skill body %d\n\nlots of text" % i)
    session(tmp_path, "auto", "run1", body)
    g = C.scan(root=str(tmp_path), now=NOW)["groups"][0]
    assert g["shown"][0]["humanSeen"] == 1
    assert g["humanish"] == 0


@pytest.mark.parametrize("prefix", [
    "<system-reminder>x</system-reminder>", "<command-name>foo</command-name>",
    "<command-message>bar</command-message>", "<local-command-stdout>z",
    "Caveat: The messages below", "[Request interrupted by user]"])
def test_pseudo_messages_do_not_count(tmp_path, prefix):
    # 这些确实是纯字符串,但不是人打的字。形状判据不够,还要这一层。
    session(tmp_path, "g", "a", user_typed("真的问题") + user_typed(prefix))
    g = C.scan(root=str(tmp_path), now=NOW)["groups"][0]
    assert g["shown"][0]["humanSeen"] == 1


def test_sidechain_entries_do_not_count(tmp_path):
    # 子 agent 的转录里也有 user 条目,但那不是机主在说话。
    body = user_typed("开始") + line(type="user", cwd="C:/proj", isSidechain=True,
                                     timestamp="2026-09-07T00:00:00Z",
                                     message={"content": "子 agent 的输入"})
    session(tmp_path, "g", "a", body)
    assert C.scan(root=str(tmp_path), now=NOW)["groups"][0]["shown"][0]["humanSeen"] == 1


# ---------- 标题来源 ----------

def test_title_falls_back_to_the_first_typed_message(tmp_path):
    session(tmp_path, "g", "a", user_typed("把日志切一下"))
    row = C.scan(root=str(tmp_path), now=NOW)["groups"][0]["shown"][0]
    assert row["title"] == "把日志切一下"
    assert row["titleFrom"] == "first-message"


def test_summary_wins_over_the_first_message(tmp_path):
    body = line(type="summary", summary="给控制台加一块对话历史",
                cwd="C:/proj", timestamp="2026-09-07T00:00:00Z") + user_typed("随便说点什么")
    session(tmp_path, "g", "a", body)
    row = C.scan(root=str(tmp_path), now=NOW)["groups"][0]["shown"][0]
    assert row["title"] == "给控制台加一块对话历史"
    assert row["titleFrom"] == "summary"


def test_slug_is_used_only_when_there_is_nothing_better(tmp_path):
    session(tmp_path, "g", "a", line(type="assistant", cwd="C:/proj",
                                     slug="twinkling-sniffing-owl",
                                     timestamp="2026-09-07T00:00:00Z"))
    row = C.scan(root=str(tmp_path), now=NOW)["groups"][0]["shown"][0]
    assert row["title"] == "twinkling-sniffing-owl"
    assert row["titleFrom"] == "slug"


def test_title_source_is_always_reported(tmp_path):
    # 一个由 slug 充数的标题和一句真概括在界面上长得一样,信息量差着量级。
    session(tmp_path, "g", "a", user_typed("x"))
    session(tmp_path, "g", "b", line(type="assistant", cwd="C:/proj", slug="a-b-c",
                                     timestamp="2026-09-07T00:00:00Z"))
    rows = C.scan(root=str(tmp_path), now=NOW)["groups"][0]["shown"]
    assert {r["titleFrom"] for r in rows} == {"first-message", "slug"}


# ---------- 分组 ----------

def test_grouping_uses_the_recorded_cwd_not_the_directory_name(tmp_path):
    # 两个不同的编码目录名,记录的却是同一个真实路径:必须并成一组。
    session(tmp_path, "C--proj", "a", user_typed("x", cwd="C:/proj"))
    session(tmp_path, "C--proj-2", "b", user_typed("y", cwd="C:/proj"))
    r = C.scan(root=str(tmp_path), now=NOW)
    assert len(r["groups"]) == 1
    assert r["groups"][0]["cwd"] == "C:/proj"
    assert r["groups"][0]["count"] == 2


def test_a_transcript_without_a_cwd_gets_its_own_group(tmp_path):
    # 读不到目录的不能塞进某个看起来合理的分组里。
    session(tmp_path, "g", "a", line(type="assistant", timestamp="2026-09-07T00:00:00Z"))
    r = C.scan(root=str(tmp_path), now=NOW)
    assert r["groups"][0]["cwd"].startswith("(")


def test_groups_are_ordered_by_recency(tmp_path):
    old = session(tmp_path, "g1", "a", user_typed("x", cwd="C:/old"))
    new = session(tmp_path, "g2", "b", user_typed("y", cwd="C:/new"))
    os.utime(old, (NOW - 90000, NOW - 90000))
    os.utime(new, (NOW - 60, NOW - 60))
    r = C.scan(root=str(tmp_path), now=NOW)
    assert [g["cwd"] for g in r["groups"]] == ["C:/new", "C:/old"]


# ---------- 诚实 ----------

def test_partial_is_reported_for_a_file_bigger_than_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "HEAD_BYTES", 200)
    monkeypatch.setattr(C, "TAIL_BYTES", 200)
    session(tmp_path, "g", "a", user_typed("x" * 3000))
    row = C.scan(root=str(tmp_path), now=NOW)["groups"][0]["shown"][0]
    # 只看了两头就不能把计数报成确定的总数。
    assert row["partial"] is True


def test_small_file_is_not_partial(tmp_path):
    session(tmp_path, "g", "a", user_typed("短"))
    assert C.scan(root=str(tmp_path), now=NOW)["groups"][0]["shown"][0]["partial"] is False


def test_group_truncation_is_flagged(tmp_path):
    for i in range(5):
        session(tmp_path, "g", "s%d" % i, user_typed("x", cwd="C:/proj"))
    r = C.scan(root=str(tmp_path), now=NOW, limit_per_group=2)
    g = r["groups"][0]
    assert len(g["shown"]) == 2 and g["count"] == 5 and g["truncated"] is True


# ---------- 缓存 ----------

def test_cache_hits_only_when_mtime_and_size_both_match(tmp_path):
    """大小也要比,不能只比 mtime。

    这条曾经因为测试自己写坏而形同虚设:第一次扫描时没有把 mtime 固定,于是两次读到的
    mtime 本来就不同,「大小变了」这一半根本没被检验到,投毒时照样全绿。
    所以下面两次写入都把 mtime 钉在同一个 T 上,让 mtime 这一半必然相等。
    """
    T = NOW - 5000
    f = session(tmp_path, "g", "a", user_typed("原文"))
    os.utime(f, (T, T))
    cache = tmp_path / "cache.json"
    first = C.scan(root=str(tmp_path), cache=str(cache), now=NOW)
    assert first["summary"]["cacheMisses"] == 1
    second = C.scan(root=str(tmp_path), cache=str(cache), now=NOW)
    assert second["summary"]["cacheHits"] == 1

    # 内容换成长度不同的一份,mtime 钉回同一个 T:只看 mtime 的缓存会给出过期答案。
    f.write_text(user_typed("改过的内容,这一份明显更长一些用来让字节数不同"), encoding="utf-8")
    os.utime(f, (T, T))
    third = C.scan(root=str(tmp_path), cache=str(cache), now=NOW)
    assert third["summary"]["cacheMisses"] == 1, "大小变了却算了缓存命中"
