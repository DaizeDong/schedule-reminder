#!/usr/bin/env python3
"""Hermetic tests for relay.py (Agent Center multi-stream egress). No network."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import relay  # noqa: E402


def _registry(tmp_path, streams):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"guild_id": "1", "streams": streams}), encoding="utf-8")
    return str(p)


def test_missing_registry_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", str(tmp_path / "nope.json"))
    assert relay.load_registry() == {}


def test_relay_known_stream_dryrun(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG",
                       _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t", "username": "mail"}}))
    monkeypatch.setenv("AGENT_CENTER_RELAY_DRYRUN", "1")
    assert relay.relay("mail", "hello") is True


def test_unknown_stream_falls_back_to_big_brother(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t"}}))
    calls = {}
    monkeypatch.setattr(relay, "_big_brother", lambda t: (calls.update(text=t), True)[1])
    assert relay.relay("ghost", "boo") is True
    assert "[ghost]" in calls["text"]  # stream name preserved for context


def test_digest_uses_big_brother(monkeypatch):
    seen = {}
    monkeypatch.setattr(relay, "_big_brother", lambda t: (seen.update(t=t), True)[1])
    assert relay.digest("daily summary") is True
    assert seen["t"] == "daily summary"


def test_list_never_leaks_webhook(monkeypatch, tmp_path, capsys):
    secret = "deadbeefSECRETtok123"
    monkeypatch.setenv("AGENT_CENTER_CONFIG",
                       _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/9/" + secret, "username": "mail"}}))
    rc = relay.main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert secret not in out  # the webhook token must never appear in `list` output
    assert "mail" in out  # but the stream name + safe metadata is shown


def test_health_ok_and_bad(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CENTER_CONFIG",
                       _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t"}}))
    assert relay.main(["health"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _registry(tmp_path, {"mail": {"webhook": "not-a-url"}}))
    assert relay.main(["health"]) == 1


def test_send_cli_dryrun(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CENTER_CONFIG",
                       _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t", "username": "mail"}}))
    monkeypatch.setenv("AGENT_CENTER_RELAY_DRYRUN", "1")
    assert relay.main(["send", "--stream", "mail", "--text", "hi"]) == 0
    assert relay.main(["send", "--stream", "mail", "--json", '{"content":"x","username":"u"}']) == 0
