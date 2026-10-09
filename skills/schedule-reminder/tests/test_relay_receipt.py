#!/usr/bin/env python3
"""`relay.py send --idempotency-key`: one receipt line a durable caller can verify. No network."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import relay  # noqa: E402

KEY = "synthetic-action-key-1"


def _registry(tmp_path, streams):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"guild_id": "1", "streams": streams}), encoding="utf-8")
    return str(p)


def _receipt(capsys):
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "receipt mode printed nothing"
    return json.loads(lines[-1])


def test_the_email_monitor_argument_shape_parses_and_confirms(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CENTER_CONFIG",
                       _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t", "username": "mail"}}))
    monkeypatch.setenv("AGENT_CENTER_RELAY_DRYRUN", "1")
    rc = relay.main(["send", "--stream", "mail", "--text", "Synthetic alert",
                     "--idempotency-key", KEY, "--receipt-adapter", "alert"])
    receipt = _receipt(capsys)
    assert rc == 0
    assert receipt["status"] == "confirmed" and receipt["idempotency_key"] == KEY
    assert receipt["adapter"] == "alert" and receipt["receipt_id"].strip()


def test_stream_without_transport_is_refused_not_sent_to_the_fallback(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t"}}))
    sent = []
    monkeypatch.setattr(relay, "_big_brother", lambda text: sent.append(text) or True)
    monkeypatch.setattr(relay, "deliver", lambda *a, **k: sent.append(a) or {})
    rc = relay.main(["send", "--stream", "ghost", "--text", "Synthetic alert",
                     "--idempotency-key", KEY, "--receipt-adapter", "alert"])
    receipt = _receipt(capsys)
    assert rc == 1 and sent == []
    assert receipt["status"] == "not_applied" and receipt["evidence"].strip()
    assert receipt["idempotency_key"] == KEY and receipt["adapter"] == "alert"


def test_a_failure_during_delivery_is_uncertain(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t"}}))

    def broken(stream, content):
        raise OSError("synthetic connection reset")
    monkeypatch.setattr(relay, "deliver", broken)
    rc = relay.main(["send", "--stream", "mail", "--text", "Synthetic alert",
                     "--idempotency-key", KEY, "--receipt-adapter", "alert"])
    receipt = _receipt(capsys)
    assert rc == 1 and receipt["status"] == "uncertain" and "receipt_id" not in receipt


@pytest.mark.parametrize("extra", [["--channel-id", "123"], []])
def test_non_stream_or_blank_requests_send_nothing(monkeypatch, tmp_path, capsys, extra):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t"}}))
    monkeypatch.setattr(relay, "deliver", lambda *a, **k: pytest.fail("nothing may be sent"))
    argv = ["send", "--stream", "mail"] + extra + ["--text", " " if not extra else "x",
                                                   "--idempotency-key", KEY]
    relay.main(argv)
    receipt = _receipt(capsys)
    assert receipt["status"] == "not_applied" and receipt["adapter"] == "relay"


def test_plain_send_is_unchanged(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_CENTER_CONFIG", _registry(tmp_path, {"mail": {"webhook": "https://h/api/webhooks/1/t"}}))
    monkeypatch.setenv("AGENT_CENTER_RELAY_DRYRUN", "1")
    assert relay.main(["send", "--stream", "mail", "--text", "Synthetic note"]) == 0
    assert "DRYRUN" in capsys.readouterr().out
