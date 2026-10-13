#!/usr/bin/env python3
"""Guards for the notification egress (the 2026-07-01 Agent Center decision).

Reminders land in the Agent Center #reminders channel through this repo's relay module, called in
the same process. No python child is started per reminder, and the relay's message-ID receipt
reaches the tick.

SCHEDULE_RELAY_CMD is the one override and the test seam: it must keep winning, or every tick
test in test_contract.py would push to real Discord.

Run: pytest -q
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import notify as notify_mod      # noqa: E402
import relay as relay_mod        # noqa: E402

RECEIPT = {"kind": "discord-message", "receipt_id": "100", "delivered": True, "exit_code": 0}


def _no_children(monkeypatch):
    """Fail if notify starts any process; clear the routing environment."""
    def forbidden(*args, **kwargs):
        raise AssertionError("notify must not start a child process without SCHEDULE_RELAY_CMD")
    monkeypatch.setattr(notify_mod.subprocess, "run", forbidden)
    for var in ("SCHEDULE_RELAY_CMD", "SCHEDULE_RELAY_STREAM"):
        monkeypatch.delenv(var, raising=False)


def test_deliver_calls_relay_in_process_and_keeps_the_receipt(monkeypatch):
    _no_children(monkeypatch)
    seen = []
    monkeypatch.setattr(relay_mod, "deliver", lambda stream, text: seen.append((stream, text)) or RECEIPT)
    assert notify_mod.deliver("hi") == RECEIPT
    assert seen == [("reminders", "hi")]


def test_text_notify_goes_to_relay_reminders_channel(monkeypatch):
    _no_children(monkeypatch)
    seen = []
    monkeypatch.setattr(relay_mod, "relay", lambda stream, text: seen.append((stream, text)) or True)
    assert notify_mod.notify("hi") is True
    assert seen == [("reminders", "hi")]


def test_stream_is_configurable(monkeypatch):
    _no_children(monkeypatch)
    monkeypatch.setenv("SCHEDULE_RELAY_STREAM", "infra")
    seen = []
    monkeypatch.setattr(relay_mod, "deliver", lambda stream, text: seen.append(stream) or RECEIPT)
    notify_mod.deliver("hi")
    assert seen == ["infra"]


def test_relay_cmd_override_still_wins(monkeypatch):
    seen = {}

    class _R:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _R()

    monkeypatch.setattr(notify_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("SCHEDULE_RELAY_CMD", "python stub.py")
    monkeypatch.setattr(relay_mod, "relay", lambda *a: (_ for _ in ()).throw(AssertionError("relay used")))
    assert notify_mod.notify("hi") is True
    assert seen["argv"] == ["python", "stub.py", "hi"]
    assert notify_mod.deliver("hi") is True


def test_text_notify_failure_returns_false_never_raises(monkeypatch):
    _no_children(monkeypatch)
    def boom(stream, text):
        raise OSError("synthetic transport failure")
    monkeypatch.setattr(relay_mod, "relay", boom)
    assert notify_mod.notify("hi") is False
    monkeypatch.setattr(relay_mod, "relay", lambda stream, text: False)
    assert notify_mod.notify("hi") is False
