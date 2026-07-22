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
  3. bigbrother.send_dm  -- the native Big Brother DM, only when relay.py is absent (standalone
     install). Replaces the retired shell-out to the legacy DM notifier script.

Run: pytest -q
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import notify as notify_mod      # noqa: E402
import bigbrother as bb_mod      # noqa: E402


def _capture(monkeypatch, rc=0):
    """Record the argv notify() would execute instead of running it."""
    seen = {}

    class _R:
        returncode = rc

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _R()

    monkeypatch.setattr(notify_mod.subprocess, "run", fake_run)
    for var in ("SCHEDULE_RELAY_CMD", "SCHEDULE_RELAY_PY", "SCHEDULE_RELAY_STREAM"):
        monkeypatch.delenv(var, raising=False)
    return seen


def _stub_dm(monkeypatch, result=True):
    """Record calls to the native Big Brother DM instead of hitting Discord."""
    dm = {}
    monkeypatch.setattr(bb_mod, "send_dm", lambda text: (dm.update(text=text), result)[1])
    return dm


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
    """Regression: the DM must not be chosen while relay.py exists (reminders go to the channel)."""
    _capture(monkeypatch)
    dm = _stub_dm(monkeypatch)
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: True)  # relay.py exists
    notify_mod.notify("hi")
    assert dm == {}, "the native DM must not fire while relay.py is available"


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


def test_falls_back_to_native_dm_when_relay_missing(monkeypatch):
    """Standalone install (no relay.py): the reminder still gets delivered via the native DM."""
    _capture(monkeypatch)
    dm = _stub_dm(monkeypatch, result=True)
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: False)  # no relay.py
    assert notify_mod.notify("hi") is True
    assert dm.get("text") == "hi", "must fall back to bigbrother.send_dm"


def test_returns_false_when_nothing_delivers(monkeypatch):
    """No relay.py and the DM fails -> False, never raises."""
    _capture(monkeypatch)
    _stub_dm(monkeypatch, result=False)
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: False)
    assert notify_mod.notify("hi") is False


def test_delivery_failure_returns_false_never_raises(monkeypatch):
    seen = _capture(monkeypatch, rc=1)
    monkeypatch.setattr(notify_mod.os.path, "isfile", lambda p: p.endswith("relay.py"))
    assert notify_mod.notify("hi") is False
    assert seen["argv"]  # it did try
