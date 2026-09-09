#!/usr/bin/env python3
"""一个任务被写进两个大类时,页面上的数字必须仍然自洽,而且这件事要被说出来。

分类配置是仓外的手写 JSON,复制粘贴一行就能让一个任务落在两个大类里,
而以前没有任何东西会说出来。后果是同一屏上「总数 40」和「41/41 行」并存:
那个任务在表里出现两行、在热力图里出现两行、在两个大类的评分里各贡献一次分母,
而所有数字都还在正常渲染 —— 看不出哪一份是对的。

这里跑的是真的 `build_payload()`,读真实的机器状态,只把分类配置换成一份合成的。
不 mock 掉任务来源,是因为这个缺陷正是在「配置」和「任务表」的接缝上:
两边都自己造的测试只能证明我写的那段 if 语句自己前后一致。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import server  # noqa: E402


def _write_cats(tmp_path, monkeypatch, cats):
    p = tmp_path / "categories.json"
    p.write_text(json.dumps({"categories": cats}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_CATEGORIES", str(p))
    return p


@pytest.fixture(scope="module")
def some_task_names():
    """机器上真实存在的两个任务名。造名字没用:分类里写一个不存在的任务本来就不该出现在表里,
    那样测的是另一条分支。"""
    payload = server.build_payload()
    names = [r["name"] for g in payload.get("groups", []) for r in g["rows"]]
    if len(names) < 2:
        pytest.skip("这台机器上的计划任务不足两个")
    return names[:2]


def test_duplicate_membership_is_reported_and_counted_once(
        tmp_path, monkeypatch, some_task_names):
    a, b = some_task_names
    _write_cats(tmp_path, monkeypatch, [
        {"name": "甲", "tasks": [a, b]},
        {"name": "乙", "tasks": [a]},          # a 被写了两遍
    ])
    d = server.build_payload()
    rows = [r["name"] for g in d["groups"] for r in g["rows"]]

    assert rows.count(a) == 1, f"{a} 出现了 {rows.count(a)} 次,行数会和总数对不上"
    assert len(rows) == len(set(rows)), "有任务在表里出现了多行"
    assert len(rows) == d["summary"]["total"], (
        f"分组行数 {len(rows)} 和顶部总数 {d['summary']['total']} 对不上 —— "
        f"同一屏上两个自称权威的数字")

    # 挑一个不能靠「我改的那段代码返回了什么」来满足的断言:警告里必须点名到具体任务。
    hit = [w for w in d.get("warnings", []) if a in w and "多个大类" in w]
    assert hit, f"没有任何警告提到 {a} 被写进了多个大类。warnings={d.get('warnings')}"
    assert "甲" in hit[0] and "乙" in hit[0], f"警告没说清是哪两个大类: {hit[0]}"

    # 只认第一个 : 归属必须是确定的,不能看字典顺序或运行顺序。
    where = {r["name"]: g["cat"] for g in d["groups"] for r in g["rows"]}
    assert where[a] == "甲", f"{a} 落在了 {where[a]},预期第一个出现的大类「甲」"


def test_no_duplicates_means_no_warning(tmp_path, monkeypatch, some_task_names):
    """正对照:配置干净时不能报警。

    一个见谁都叫的闸门会被无视,那和没有闸门是同一个结果。
    """
    a, b = some_task_names
    _write_cats(tmp_path, monkeypatch, [
        {"name": "甲", "tasks": [a]},
        {"name": "乙", "tasks": [b]},
    ])
    d = server.build_payload()
    assert not [w for w in d.get("warnings", []) if "多个大类" in w]
    rows = [r["name"] for g in d["groups"] for r in g["rows"]]
    assert len(rows) == len(set(rows)) == d["summary"]["total"]
