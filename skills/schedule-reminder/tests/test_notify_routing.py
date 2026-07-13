#!/usr/bin/env python3
"""Guards for the notification egress (the 2026-07-01 Agent Center decision).

Reminders must land in the Agent Center **#reminders channel** via this repo's `relay.py`, not in
the Big Brother DM. The agent-center-hub `push.py` exploration was ARCHIVED and never adopted --
`relay.py` is the single egress -- but the tick's `notify.py` was never migrated and kept posting
to the DM. This locks the routing in.

Precedence (first that exists wins):
  1. SCHEDULE_RELAY_CMD  -- explicit override AND the test seam (must keep winning, or every
     tick test in test_contract.py would start pushing to real Discord).
  2. relay.py send --stream reminders
  3. send.py (legacy DM) -- only when relay.py is absent.

Run: pytest -q
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import notify as notify_mod  # noqa: E402


def _capture(monkeypatch, rc=0):
    """Record the argv notify() would execute instead of running it."""
    seen = {}

    class _R:
        returncode = rc

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _R()

    monkeypatch.setattr(notify_mod.subprocess, "run", fake_run)
    for var in ("SCHEDULE_RELAY_CMD", "SCHEDULE_RELAY_PY", "SCHEDULE_RELAY_STREAM",
                "SCHEDULE_RELAY_SEND"):
        monkeypatch.delenv(var, raising=False)
    return seen


def test_default_goes_to_relay_reminders_channel(monkeypatch):
    """The default egress is the Agent Center #reminders channel, NOT the DM."""
    seen = _capture(monkeypatch)
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: p.endswith("relay.py"))
    assert notify_mod.notify("hi") is True
    argv = seen["argv"]
    assert any(a.endswith("relay.py") for a in argv), "must route through relay.py"
    assert "send" in argv and "--stream" in argv
    assert argv[argv.index("--stream") + 1] == "reminders"
    assert argv[argv.index("--text") + 1] == "hi"


def test_default_is_not_the_big_brother_dm(monkeypatch):
    """Regression: send.py (DM) must not be chosen while relay.py exists."""
    seen = _capture(monkeypatch)
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: True)  # both exist
    notify_mod.notify("hi")
    assert not any(a.endswith("send.py") for a in seen["argv"])


def test_relay_cmd_override_still_wins(monkeypatch):
    """The test seam must keep top priority — otherwise the tick tests hit real Discord."""
    seen = _capture(monkeypatch)
    monkeypatch.setenv("SCHEDULE_RELAY_CMD", "python stub.py")
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: True)
    assert notify_mod.notify("hi") is True
    assert seen["argv"] == ["python", "stub.py", "hi"]


def test_stream_is_configurable(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setenv("SCHEDULE_RELAY_STREAM", "infra")
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: p.endswith("relay.py"))
    notify_mod.notify("hi")
    argv = seen["argv"]
    assert argv[argv.index("--stream") + 1] == "infra"


def test_falls_back_to_dm_when_relay_missing(monkeypatch):
    """Standalone install (no relay.py): the reminder still gets delivered, never dropped."""
    seen = _capture(monkeypatch)
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: p.endswith("send.py"))
    assert notify_mod.notify("hi") is True
    assert seen["argv"][-1] == "hi"
    assert any(a.endswith("send.py") for a in seen["argv"])


def test_returns_false_when_no_channel_exists(monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: False)
    assert notify_mod.notify("hi") is False


def test_delivery_failure_returns_false_never_raises(monkeypatch):
    seen = _capture(monkeypatch, rc=1)
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: p.endswith("relay.py"))
    assert notify_mod.notify("hi") is False
    assert seen["argv"]  # it did try
