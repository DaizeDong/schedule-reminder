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
