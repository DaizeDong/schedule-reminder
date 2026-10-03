#!/usr/bin/env python3
"""Heartbeat controls for unbounded repetition and Python streams without a console."""
import io
import os
import re
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import reminder  # noqa: E402
import store  # noqa: E402


# ---------- 1. the heartbeat must repeat forever ----------

def _install_xml():
    import capabilities
    import installer
    return installer.task_xml(capabilities.plan("remind")["capabilities"]["remind"])


def test_heartbeat_repetition_is_unbounded():
    """A <Duration> inside <Repetition> makes Windows stop the heartbeat when it elapses."""
    xml = _install_xml()
    rep = re.search(r"<Repetition>(.*?)</Repetition>", xml, re.S)
    assert rep, "install.ps1 must still register a repeating heartbeat"
    assert "<Duration>" not in rep.group(1), (
        "<Duration> bounds the repetition -- the heartbeat dies when it elapses. "
        "Omit it so the task repeats indefinitely.")


def test_heartbeat_interval_still_present():
    rep = re.search(r"<Repetition>(.*?)</Repetition>", _install_xml(), re.S).group(1)
    assert "<Interval>PT5M</Interval>" in rep


# ---------- 2. output must survive a missing stdout (pythonw) ----------

def test_emit_does_not_raise_when_stdout_is_none(monkeypatch):
    """pythonw.exe: sys.stdout is None. Reporting must not kill a completed operation."""
    monkeypatch.setattr(sys, "stdout", None)
    assert reminder._emit({"dispatched": []}) == 0  # must return success, not raise


def test_fail_does_not_raise_when_stderr_is_none(monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    assert reminder._fail(store.SkillError("ERR_NOT_FOUND", "nope")) == 1


def test_emit_and_fail_both_none_no_raise(monkeypatch):
    """The real pythonw case: BOTH streams are None. This is what escaped and exited 1."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert reminder._emit({"ok": True}) == 0
    assert reminder._fail(RuntimeError("boom")) == 1


def test_emit_still_writes_json_when_stdout_exists(monkeypatch):
    """The contract is unchanged for real callers (subprocess with a pipe)."""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    assert reminder._emit({"dispatched": ["x"]}) == 0
    import json
    out = json.loads(buf.getvalue())
    assert out["ok"] is True and out["dispatched"] == ["x"]
    assert out["api_version"] == store.API_VERSION


def test_fail_still_writes_json_when_stderr_exists(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    assert reminder._fail(store.SkillError("ERR_NOT_FOUND", "nope")) == 1
    import json
    out = json.loads(buf.getvalue())
    assert out["ok"] is False and out["error_code"] == "ERR_NOT_FOUND"


# ---------- 3. 源码和注册态是同一个事实的两份副本 ----------

def test_the_registered_task_matches_what_install_ps1_declares():
    """Compare an available Windows registration with the expected unbounded heartbeat.
    
    An absent registration cannot establish live readback and remains an explicit platform skip."""
    import subprocess
    if os.name != "nt":
        pytest.skip("只在 Windows 上有注册态可查")
    r = subprocess.run(["schtasks", "/query", "/tn", "ScheduleReminderTick", "/xml"],
                       capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        pytest.skip("这台机器上没有注册这个任务")
    live = r.stdout.decode("utf-16", "replace")
    if "<Repetition>" not in live:
        live = r.stdout.decode("utf-8", "replace")
    rep = re.search(r"<Repetition>(.*?)</Repetition>", live, re.S)
    assert rep, "注册态里没有 <Repetition> —— 心跳不再重复"
    body = rep.group(1)
    assert "<Duration>" not in body, (
        "**已注册的**任务里有 <Duration>:Windows 会在它走完之后永远停止重复。"
        "源码修好不算修好,要重装那个任务。")
    assert "<Interval>PT5M</Interval>" in body, f"注册态的间隔不是 PT5M: {body.strip()[:120]}"
