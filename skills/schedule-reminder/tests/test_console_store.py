#!/usr/bin/env python3
"""console_store 的连接契约。

这个文件之前不存在 —— 而 `connect_ro` 是**每次 HTTP 请求**都会走的那个函数,
它的异常路径零覆盖。零覆盖不会以任何方式报出来。

## 关于「CORRUPT 路径泄漏连接」这条发现:实测**不成立**

审计报过一条 medium:探针 `SELECT 1 FROM meta` 抛异常时直接 return,而连接已经建起来了,
调用方拿到的是 `(None, DbState)`,永远不可能替它关 —— 于是每次页面刷新泄漏一个句柄。
推理是通的,对抗性核证也判它成立。**但它是错的。**

实测(2026-09-09,本机,带对照组):

    对照组(故意打开 200 个文件不关): 句柄 94 -> 294,差 200   <- 测量方法有效
    connect_ro 走坏库 200 次:          句柄 94 -> 94, 差 0     <- 没有泄漏

CPython 的引用计数在函数返回时就把那个局部变量的连接析构掉了,`sqlite3.Connection`
的析构会关掉底层句柄。**带对照组这件事是必须的**:没有它,「两边都是 0」既符合
「没漏」也符合「我根本没测到」—— 第一次量的时候我用 ctypes 拿句柄数,拿回来的就是 0/0,
那不是结论,那是没测到。

代码里仍然显式 `con.close()`,理由不是「修泄漏」,而是**不依赖引用计数的时机**:
那是实现细节,而且将来只要有一条路径在异常前把连接存进了别处(重试、缓存、日志),
显式关就从多余变成必须。这里的用例因此是一条**形状检查**,不是泄漏检查 ——
它能失败(把 close 删掉就红),但它证明的是「这条路上有显式关闭」,不是「否则会漏」。
"""
import os
import re
import sys

import pytest  # noqa: F401

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "task_console"))

import console_store as S  # noqa: E402


def test_the_corrupt_branch_closes_explicitly():
    """形状检查:CORRUPT 分支里要有显式的 close。

    ⚠ 这条**不是**泄漏检查(泄漏实测不存在,见文件头)。它钉的是「不依赖引用计数的时机」。
    写成形状检查而不是行为检查,是因为行为上根本量不出差别 ——
    **一条量不出差别的行为断言,写出来就是恒真的**,那正是这一轮在清理的东西。
    """
    src = open(S.__file__, encoding="utf-8").read()
    i = src.index("def connect_ro")
    body = src[i:i + 2500]
    j = body.index('DbState("CORRUPT"')
    before = body[:j]
    assert re.search(r"con\.close\(\)", before), \
        "CORRUPT 分支在 return 之前没有显式关闭连接"


def test_a_corrupt_database_is_reported_not_raised(tmp_path, monkeypatch):
    """坏库要变成一个有代码的 DbState,而不是抛出去。"""
    bad = tmp_path / "console.sqlite3"
    bad.write_bytes(b"this is definitely not a sqlite database" * 40)
    monkeypatch.setenv("TASK_CONSOLE_DB", str(bad))
    con, st = S.connect_ro()
    assert con is None
    assert st is not None and st.code == "CORRUPT", st
    assert "backfill" in st.message or "重建" in st.message, st.message


def test_a_missing_database_is_a_different_code(tmp_path, monkeypatch):
    """负对照:文件不在和文件坏了必须是两个不同的答案。

    折叠它们会让「还没建过库」和「库坏了」变成同一句话,而这两件事要做的事不同。
    """
    monkeypatch.setenv("TASK_CONSOLE_DB", str(tmp_path / "never-made.sqlite3"))
    con, st = S.connect_ro()
    assert con is None and st and st.code == "NO_FILE", st


def test_a_healthy_database_still_returns_a_usable_connection(tmp_path, monkeypatch):
    """正对照:库正常时照样要拿到一个能用的连接。

    少了这一条,一个「永远返回 None 并关掉一切」的实现也能让上面几条通过。
    """
    import console_ingest as CI
    monkeypatch.setenv("TASK_CONSOLE_DB", str(tmp_path / "ok.sqlite3"))
    con, path = CI.open_rw()
    assert con, path
    con.commit()
    con.close()
    con2, st = S.connect_ro()
    assert st is None and con2 is not None
    assert con2.execute("SELECT 1 FROM meta LIMIT 1") is not None
    con2.close()
