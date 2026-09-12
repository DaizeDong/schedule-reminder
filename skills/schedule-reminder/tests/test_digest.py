#!/usr/bin/env python3
"""Hermetic tests for digest.py (daily 当日总结 aggregator). No network."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import digest  # noqa: E402
import relay  # noqa: E402

PY = sys.executable


def _set_contribs(monkeypatch, tmp_path, contributors):
    p = tmp_path / "digest.json"
    p.write_text(json.dumps({"contributors": contributors}), encoding="utf-8")
    monkeypatch.setenv("AGENT_CENTER_DIGEST", str(p))
    return str(p)


def test_aggregates_sections_and_tolerates_failure(monkeypatch, tmp_path, capsys):
    _set_contribs(monkeypatch, tmp_path, [
        {"name": "email", "title": "MAIL", "cmd": [PY, "-c", "print('m1')"], "enabled": True},
        {"name": "hot", "title": "HOT", "cmd": [PY, "-c", "print('h1')"], "enabled": True},
        {"name": "bad", "title": "BAD", "cmd": [PY, "-c", "import sys;sys.exit(2)"], "enabled": True},
    ])
    rc = digest.run(now="2026-06-27T22:00:00Z", dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "当日总结 · 2026-06-27" in out
    assert "MAIL" in out and "m1" in out and "HOT" in out and "h1" in out
    assert "BAD" not in out  # failing contributor's section omitted
    assert "bad:" in out  # but reported in the problems line


def test_empty_section_skipped(monkeypatch, tmp_path, capsys):
    _set_contribs(monkeypatch, tmp_path, [
        {"name": "blank", "title": "BLANK", "cmd": [PY, "-c", "pass"], "enabled": True},
    ])
    digest.run(now="2026-06-27T00:00:00Z", dry_run=True)
    out = capsys.readouterr().out
    assert "今日各来源无内容" in out
    assert "BLANK" not in out


def test_disabled_contributor_skipped(monkeypatch, tmp_path, capsys):
    _set_contribs(monkeypatch, tmp_path, [
        {"name": "on", "title": "ON", "cmd": [PY, "-c", "print('yes')"], "enabled": True},
        {"name": "off", "title": "OFF", "cmd": [PY, "-c", "print('no')"], "enabled": False},
    ])
    digest.run(now="2026-06-27T00:00:00Z", dry_run=True)
    out = capsys.readouterr().out
    assert "ON" in out and "yes" in out
    assert "OFF" not in out and "no" not in out


def test_no_contributors_note(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CENTER_DIGEST", str(tmp_path / "absent.json"))
    digest.run(now="2026-06-27T00:00:00Z", dry_run=True)
    out = capsys.readouterr().out
    assert "暂无已注册的当日总结贡献者" in out


def test_register_unregister_roundtrip(monkeypatch, tmp_path):
    _set_contribs(monkeypatch, tmp_path, [])
    assert digest.main(["register", "--name", "x", "--title", "X", "--cmd", PY + " -c print(1)"]) == 0
    d = json.loads((tmp_path / "digest.json").read_text(encoding="utf-8"))
    assert any(c["name"] == "x" for c in d["contributors"])
    # idempotent replace (same name -> still one)
    digest.main(["register", "--name", "x", "--title", "X2", "--cmd", PY + " -c print(2)"])
    d = json.loads((tmp_path / "digest.json").read_text(encoding="utf-8"))
    assert sum(1 for c in d["contributors"] if c["name"] == "x") == 1
    assert digest.main(["unregister", "--name", "x"]) == 0
    d = json.loads((tmp_path / "digest.json").read_text(encoding="utf-8"))
    assert not any(c["name"] == "x" for c in d["contributors"])


def test_collect_emits_sections_only(monkeypatch, tmp_path, capsys):
    _set_contribs(monkeypatch, tmp_path, [
        {"name": "a", "title": "A", "cmd": [PY, "-c", "print('aa')"], "enabled": True},
    ])
    monkeypatch.setattr(relay, "relay", lambda *a, **k: True)
    rc = digest.collect(now="2026-06-27T00:00:00Z")
    out = capsys.readouterr().out
    assert rc == 0
    assert "A" in out and "aa" in out
    assert "当日总结" not in out  # collect emits sections only, no top header


def test_collect_says_so_when_nobody_is_registered(monkeypatch, tmp_path, capsys):
    """空名单必须出声,不能打印空串。

    这条用例原来断言的是 `out.strip() == ""` —— 它钉住的正是后来查出来的那个缺陷,
    所以是**故意改掉的**,不是为了让红变绿。宿主脚本用
    `if ($skillDigest) { 加这一段 }` 拼消息,打印空串就等于整段从夜里那条消息里消失,
    而消失的一段和「聚合器压根没跑」在屏幕上一模一样。
    实测 2026-09-11:这个聚合器接进生产好几个月,contributors 一直是空数组,
    于是那一段每天都不出现,没有任何人看得出来。
    """
    monkeypatch.setenv("AGENT_CENTER_DIGEST", str(tmp_path / "none.json"))
    rc = digest.collect(now="2026-06-27T00:00:00Z")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip(), "空名单时打印了空串,那一段会从宿主消息里整段消失"
    assert "来源 0" in out


def test_run_delivers_via_relay_digest(monkeypatch, tmp_path):
    _set_contribs(monkeypatch, tmp_path, [
        {"name": "a", "title": "A", "cmd": [PY, "-c", "print('body')"], "enabled": True},
    ])
    captured = {}
    monkeypatch.setattr(relay, "digest", lambda text: captured.setdefault("text", text) or True)
    monkeypatch.setattr(relay, "relay", lambda *a, **k: True)
    rc = digest.run(now="2026-06-27T00:00:00Z", dry_run=False)
    assert rc == 0
    assert "A" in captured["text"] and "body" in captured["text"]


# ---------------------------------------------------------------- 名册:谁没答上来
# 一份插件式聚合出来的日报可以用三种方式变安静,而写成消息之后三种长得一模一样:
# 跑了但没话说、跑挂了、注册着但被停用。只有第一种是「今天没事」。
# 拆任务之后这条尤其要紧:今天那条消息由必定会跑的备份任务携带,拆完就没有任何东西保证它到达。

def test_a_failed_source_is_named_in_the_message_not_only_on_another_channel(
        monkeypatch, tmp_path, capsys):
    """失败要出现在**这条消息里**。

    原来失败只往 #infra 发一条。那是另一个频道,读这条日报的人看不到,
    而这条日报正是用来回答「今天大家都报了吗」的那一条。
    """
    _set_contribs(monkeypatch, tmp_path, [
        {"name": "ok", "title": "OK", "cmd": [PY, "-c", "print('fine')"], "enabled": True},
        {"name": "boom", "title": "BOOM", "cmd": [PY, "-c", "import sys;sys.exit(3)"],
         "enabled": True},
    ])
    digest.collect(now="2026-06-27T00:00:00Z")
    out = capsys.readouterr().out
    assert "fine" in out
    assert "没答上来" in out and "boom" in out


def test_a_registered_but_silent_source_is_counted_not_forgotten(
        monkeypatch, tmp_path, capsys):
    """跑了、退出 0、没输出 —— 合法,但必须数出来。

    「明确无内容」和「没被跑到」是两件事,而只看段落的话两者都是「没有这一段」。
    """
    _set_contribs(monkeypatch, tmp_path, [
        {"name": "quiet", "title": "QUIET", "cmd": [PY, "-c", "pass"], "enabled": True},
    ])
    digest.collect(now="2026-06-27T00:00:00Z")
    out = capsys.readouterr().out
    assert "明确无内容" in out and "quiet" in out


def test_a_disabled_source_is_named_so_it_cannot_be_forgotten(monkeypatch, tmp_path, capsys):
    """停用一天很正常,停用半年没人记得就不正常了。名册每天把它念出来。"""
    _set_contribs(monkeypatch, tmp_path, [
        {"name": "on", "title": "ON", "cmd": [PY, "-c", "print('x')"], "enabled": True},
        {"name": "paused", "title": "PAUSED", "cmd": [PY, "-c", "print('y')"], "enabled": False},
    ])
    digest.collect(now="2026-06-27T00:00:00Z")
    out = capsys.readouterr().out
    assert "已停用" in out and "paused" in out
    assert "y" not in out, "停用的来源不该真的被执行"


def test_the_roster_appears_on_a_completely_quiet_day(monkeypatch, tmp_path, capsys):
    """全都无内容时也要有那一行。

    一行只在出事时才出现的提示,会把「它没出现」训练成好消息,
    而「它没出现」同样可以是聚合器根本没跑。
    """
    _set_contribs(monkeypatch, tmp_path, [
        {"name": "a", "title": "A", "cmd": [PY, "-c", "pass"], "enabled": True},
    ])
    digest.collect(now="2026-06-27T00:00:00Z")
    out = capsys.readouterr().out
    assert out.strip(), "安静的一天打印了空串"
    assert "来源 1" in out


def test_run_also_carries_the_roster(monkeypatch, tmp_path, capsys):
    """独立发送那条路(不折叠进别的推送时用)同样要带名册。"""
    _set_contribs(monkeypatch, tmp_path, [
        {"name": "z", "title": "Z", "cmd": [PY, "-c", "import sys;sys.exit(1)"], "enabled": True},
    ])
    digest.run(now="2026-06-27T00:00:00Z", dry_run=True)
    out = capsys.readouterr().out
    assert "没答上来" in out and "z" in out
