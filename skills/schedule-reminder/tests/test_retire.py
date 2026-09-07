#!/usr/bin/env python3
"""任务退役的测试。

这个动作会改两个真实配置文件,所以测试的重点在四件事上:
  拒绝(名字不合法、没写原因、配置缺一处就不许只做一半)
  幂等(已经不在名单里的,报 already 而不是假装改了)
  健康清单写回后必须仍然是**纯 ASCII**
  allow-list 里只删掉那一行,别的名字一个不能少

最后一条不是形式主义:治理规范里记过一次实测事故,按字面比对把一个活任务的启动器
当成孤儿删掉了。这里的对应形态是「删 A 的时候顺手带走了 AB」。

Windows 那一步(停用 + 写 Description)不在这里跑,它需要真的任务计划。
测试把它替换掉,断言的是**它有没有被调用、拿到的原因对不对**。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import retire as R  # noqa: E402
from maint import Refused  # noqa: E402

ALLOW = """# some header
$TaskNames = @(
  'Alpha',
  'AlphaBeta',
  'Gamma'
)
# trailer
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    al = tmp_path / "sync.ps1"
    al.write_text(ALLOW, encoding="utf-8")
    hp = tmp_path / "health.json"
    hp.write_text(json.dumps({"tasks": [
        {"name": "Alpha", "label": "a", "max_age_hours": 24},
        {"name": "AlphaBeta", "label": "ab", "max_age_hours": 24},
    ]}, ensure_ascii=True, indent=2), encoding="ascii")
    monkeypatch.setenv("TASK_CONSOLE_ALLOWLIST", str(al))
    monkeypatch.setenv("TASK_CONSOLE_HEALTH", str(hp))
    state = {"v": "Ready"}
    monkeypatch.setattr(R, "_task_state", lambda n: state["v"])
    calls = []
    def fake_disable(n, why):
        calls.append((n, why))
        state["v"] = "Disabled"      # 停用之后状态真的会变,桩也要反映这一点
        return True, ""

    monkeypatch.setattr(R, "_disable", fake_disable)
    return al, hp, calls


# ---------- 拒绝 ----------

@pytest.mark.parametrize("bad", ["../x", "a/b", "", "a;b", "a b"])
def test_bad_names_are_refused(cfg, bad):
    with pytest.raises(Refused) as e:
        R.plan(bad, "because")
    assert e.value.code == "bad_name"


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_a_retirement_without_a_reason_is_refused(cfg, empty):
    # 没有原因的退役,半年后没人敢重启用也没人敢删。这是规范里明写的一条。
    with pytest.raises(Refused) as e:
        R.plan("Alpha", empty)
    assert e.value.code == "no_reason"


def test_missing_config_blocks_the_whole_thing(tmp_path, monkeypatch):
    # 三处登记只改得动一处的话,结果是三处互相矛盾,比什么都不做更糟。
    monkeypatch.delenv("TASK_CONSOLE_ALLOWLIST", raising=False)
    monkeypatch.delenv("TASK_CONSOLE_HEALTH", raising=False)
    monkeypatch.setattr(R, "_task_state", lambda n: "Ready")
    with pytest.raises(Refused) as e:
        R.apply("Alpha", "架构上被判定为错误")
    assert e.value.code == "no_config"


# ---------- 计划 ----------

def test_plan_reports_each_place_separately(cfg):
    p = R.plan("Alpha", "reason")
    states = {s["step"]: s["state"] for s in p["steps"]}
    assert states == {"disable": "will-change", "allowlist": "will-change",
                      "health": "will-change"}
    assert p["changes"] == 3


def test_plan_says_already_for_a_place_that_needs_no_change(cfg):
    # 「这一处本来就不用改」和「这一处我没能力改」必须是两个不同的答案。
    p = R.plan("Gamma", "reason")            # Gamma 在 allow-list 里但不在健康清单里
    states = {s["step"]: s["state"] for s in p["steps"]}
    assert states["allowlist"] == "will-change"
    assert states["health"] == "already"


def test_plan_writes_nothing(cfg):
    al, hp, _ = cfg
    before = (al.read_text(encoding="utf-8"), hp.read_text(encoding="ascii"))
    R.plan("Alpha", "reason")
    assert (al.read_text(encoding="utf-8"), hp.read_text(encoding="ascii")) == before


# ---------- 执行 ----------

def test_apply_changes_all_three_places(cfg):
    al, hp, calls = cfg
    r = R.apply("Alpha", "架构上判定为错误")
    assert set(r["done"]) == {"disable", "allowlist", "health"}
    assert "'Alpha'," not in al.read_text(encoding="utf-8")
    assert all(t["name"] != "Alpha" for t in json.loads(hp.read_text(encoding="ascii"))["tasks"])
    assert calls and calls[0][0] == "Alpha"
    assert "架构上判定为错误" in calls[0][1]


def test_a_similarly_named_task_is_not_taken_along(cfg):
    # 治理规范里记过一次实测事故:按字面比对把一个活任务当孤儿删掉了。
    # 这里的对应形态是删 Alpha 的时候顺手带走 AlphaBeta。
    al, hp, _ = cfg
    R.apply("Alpha", "reason")
    txt = al.read_text(encoding="utf-8")
    assert "'AlphaBeta'" in txt, "同前缀的任务被一起删掉了"
    assert "'Gamma'" in txt
    names = [t["name"] for t in json.loads(hp.read_text(encoding="ascii"))["tasks"]]
    assert names == ["AlphaBeta"]


def test_health_manifest_stays_pure_ascii(cfg):
    """写回后必须仍是纯 ASCII。

    读这份清单的那一侧没有指定编码。一旦写进非 ASCII,它会在某个环节整份解码失败,
    而那等于「所有任务都没有被监控」,同时界面看起来完全正常。
    """
    al, hp, _ = cfg
    data = json.loads(hp.read_text(encoding="ascii"))
    data["tasks"].append({"name": "Delta", "label": "中文标签", "max_age_hours": 24})
    hp.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="ascii")
    R.apply("Alpha", "reason")
    raw = hp.read_bytes()
    assert all(b < 128 for b in raw), "健康清单里出现了非 ASCII 字节"
    # 正对照:内容还在,只是被转义了。
    assert b"Delta" in raw


def test_backups_are_left_behind(cfg):
    al, hp, _ = cfg
    r = R.apply("Alpha", "reason")
    assert len(r["backups"]) == 2
    for b in r["backups"]:
        assert os.path.isfile(b)


def test_apply_is_idempotent(cfg):
    al, hp, calls = cfg
    R.apply("Alpha", "reason")
    second = R.apply("Alpha", "reason")
    # 第二次没有任何一处需要改,所以 done 是空的,而不是报错或重复写。
    assert second["done"] == []


def test_disable_failure_stops_before_touching_files(cfg, monkeypatch):
    al, hp, _ = cfg
    monkeypatch.setattr(R, "_disable", lambda n, why: (False, "boom"))
    before = al.read_text(encoding="utf-8")
    with pytest.raises(Refused) as e:
        R.apply("Alpha", "reason")
    assert e.value.code == "disable_failed"
    # 停用都没成的情况下改配置文件,会造出「配置里没有、任务还在跑」的错位。
    assert al.read_text(encoding="utf-8") == before
