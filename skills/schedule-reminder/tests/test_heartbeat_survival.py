#!/usr/bin/env python3
"""Guards for the two bugs that silently killed the heartbeat (found 2026-07-13).

The base's whole promise is "a reminder you set will fire". Both of these broke that promise
*silently* -- nothing in the DB looked wrong, the items just never fired.

1. BOUNDED REPETITION. install.ps1 registered the PT5M heartbeat with `<Duration>P1D</Duration>`,
   so Windows repeated the task for exactly 24h and then stopped forever. The tick had been dead
   for 17 days (last run 2026-06-26) with NextRun empty; every reminder due after that -- including
   a brand-new one -- would simply never have fired. `StopAtDurationEnd` does NOT save you: it only
   decides whether a *running* instance is killed at the end of the duration. Omitting <Duration>
   is what makes the repetition indefinite. (email-monitor hit the identical bug and fixed it in
   its own v0.1.3; the base itself was never fixed.)

2. NO CONSOLE, NO STDOUT. The task runs under `pythonw.exe`, which has no console, so CPython sets
   `sys.stdout`/`sys.stderr` to **None**. `_emit()` wrote via `sys.stdout.write()` -> AttributeError
   -> `_fail()` then wrote via `sys.stderr.write()` -> AttributeError again -> escaped -> exit 1.
   Every scheduled tick exited 1 *after already doing its work*, so a genuine failure and a
   cannot-print were indistinguishable and the red task was ignored.

Run: pytest -q
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import reminder  # noqa: E402
import store  # noqa: E402


# ---------- 1. the heartbeat must repeat forever ----------

def _install_xml():
    with open(os.path.join(SCRIPTS, "install.ps1"), "r", encoding="utf-8-sig") as f:
        return f.read()


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
    """真正让心跳静默死掉 17 天的是**已注册任务**里的 <Duration>,不是源码里的。

    ⚠ 上面两条读的是 `install.ps1` 的文本。源码和注册态是同一个事实的两份副本,
    而它们之间**没有任何东西对账**:源码修好了、注册态没跟着重装,心跳照样是死的,
    而这两条用例仍然全绿。任何人手工在任务计划里编辑过一次,也是同一个结果。
    (2026-09-09 实测:注册态此刻没有漂,所以这是补一道对账,不是缺陷已经发生。)

    机器上没有这个任务(别的机器、CI)就跳过 —— 那时确实无从对账。
    """
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
