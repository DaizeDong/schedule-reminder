# -*- coding: utf-8 -*-
"""Agent execution tier - the safety-critical unit tests.

What is pinned here is the set of properties that, if they quietly stopped holding, would rebuild
the exact failure this tier exists to fix: a bus that reports success while nothing happened.

  triage        an instruction becomes work, and an empty one becomes nothing
  stop          only ids that are actually running may be targeted
  claim         two overlapping ticks cannot both launch the same order
  liveness      a dead process is dead, and a recycled pid is not the original
  stall         identical rounds are recognised as identical, different ones are not
  rotation      a fresh approach does not inherit the reasoning that failed
  honesty       a terminal report carries the real check output and never claims "handled"

Plus a NEGATIVE CONTROL: poison the check so it exits non-zero and prove the round refuses to close.
A test suite that only exercises the passing path cannot tell a working gate from an absent one.

No model, no Discord, no real pool: every boundary is stubbed.
"""
import json
import os
import subprocess
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import agent_run   # noqa: E402
import agent_task  # noqa: E402
import agent_tick  # noqa: E402
import dispatch    # noqa: E402
import ingest      # noqa: E402

_WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------- triage -> enqueue
def test_agent_op_enqueues_work_not_a_todo(monkeypatch):
    """The defect being fixed: an instruction used to become a to-do that nobody executed."""
    made = []
    monkeypatch.setattr(agent_task, "enqueue",
                        lambda stream, request, workspace=None, **k:
                        made.append((stream, request, workspace)) or {"id": "wo-1"})
    calls = []
    monkeypatch.setattr(dispatch, "_rem", lambda *a: calls.append(a) or {})
    plan = {"actions": [{"op": "agent", "request": "把每天早上发图那个任务停掉",
                         "workspace": None, "why": "用户要求"}]}
    res = dispatch.execute("crypto", dispatch.STREAMS["crypto"], plan, [])
    assert res["enqueued"] == ["wo-1"]
    assert made and made[0][0] == "crypto" and "停掉" in made[0][1]
    assert not any(c[0] == "add" for c in calls), "an agent op must not also create a pool to-do"


def test_agent_op_without_a_request_is_skipped(monkeypatch):
    monkeypatch.setattr(agent_task, "enqueue",
                        lambda *a, **k: pytest.fail("must not enqueue an empty request"))
    res = dispatch.execute("infra", dispatch.STREAMS["infra"],
                           {"actions": [{"op": "agent", "request": "   "}]}, [])
    assert res["enqueued"] == [] and any("agent?" in s for s in res["skipped"])


def test_enqueue_failure_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(agent_task, "enqueue", lambda *a, **k: {"_err": "ERR_BAD_JSON"})
    res = dispatch.execute("infra", dispatch.STREAMS["infra"],
                           {"actions": [{"op": "agent", "request": "do a thing"}]}, [])
    assert res["enqueued"] == [] and any("agent?" in s for s in res["skipped"])


def test_confirm_names_the_enqueued_order(monkeypatch):
    """A vague model summary must not be able to hide that an agent is now running."""
    monkeypatch.setattr(dispatch, "get_state", lambda cfg: [])
    monkeypatch.setattr(dispatch, "get_work", lambda: [])
    monkeypatch.setattr(dispatch, "call_chain", lambda *a, **k:
                        '{"actions":[{"op":"agent","request":"stop the thing"}],"confirm":"好的"}')
    monkeypatch.setattr(agent_task, "enqueue", lambda *a, **k: {"id": "abcdef1234"})
    posted = []
    monkeypatch.setattr(dispatch.relay, "relay", lambda s, t: posted.append(t) or True)
    assert dispatch.dispatch("crypto", "别发了") is True
    assert "abcdef12" in posted[0] and "派活" in posted[0]


# --------------------------------------------------------------------------- stop
def test_stop_only_targets_running_orders(monkeypatch):
    stopped = []
    monkeypatch.setattr(agent_tick, "stop",
                        lambda iid, note="", **k: stopped.append(iid) or [{"id": iid}])
    work = [{"id": "live-1", "title": "t"}]
    res = dispatch.execute("crypto", dispatch.STREAMS["crypto"],
                           {"actions": [{"op": "stop", "id": "ghost-9"}]}, [], work=work)
    assert stopped == [], "a stop for an order that is not running must not reach the killer"
    assert res["stopped"] == [] and any("stop?" in s for s in res["skipped"])

    res = dispatch.execute("crypto", dispatch.STREAMS["crypto"],
                           {"actions": [{"op": "stop", "id": "live-1"}]}, [], work=work)
    assert stopped == ["live-1"] and res["stopped"] == ["live-1"]


def test_stop_wildcard_is_allowed_without_an_id(monkeypatch):
    seen = []
    monkeypatch.setattr(agent_tick, "stop", lambda iid, note="", **k: seen.append(iid) or [])
    dispatch.execute("crypto", dispatch.STREAMS["crypto"],
                     {"actions": [{"op": "stop", "id": "*"}]}, [], work=[])
    assert seen == ["*"]


def test_stop_cancels_before_killing(monkeypatch):
    """Killing first would let the reaper see a dead process under a still-running order and report
    a crash for something the user deliberately stopped."""
    order = []
    item = {"id": "wo-9", "title": "x",
            "ext": {agent_task.EXT_PID: 4242, agent_task.EXT_PSTART: 7,
                    agent_task.EXT_STREAM: "crypto", agent_task.EXT_STATE: agent_task.STATE_RUNNING}}
    monkeypatch.setattr(agent_task, "orders", lambda active_only=True: [item])
    monkeypatch.setattr(agent_task, "cancel", lambda i, n="": order.append("cancel") or {})
    monkeypatch.setattr(agent_task, "kill_tree", lambda p, s: order.append("kill") or True)
    monkeypatch.setattr(agent_task, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(agent_tick, "_post", lambda *a, **k: None)
    agent_tick.stop("wo-9")
    assert order == ["cancel", "kill"]


# --------------------------------------------------------------------------- claim / race
def _order(oid, state=agent_task.STATE_QUEUED, **ext):
    e = {agent_task.EXT_STATE: state, agent_task.EXT_STREAM: "crypto"}
    e.update(ext)
    return {"id": oid, "title": "t", "state": "pending", "ext": e,
            "updated_at": "2020-01-01T00:00:00Z"}


def test_claim_uses_compare_and_swap_on_pending(monkeypatch):
    seen = {}
    monkeypatch.setattr(agent_task, "rem",
                        lambda *a: seen.update(args=a) or {"item": {"state": "doing"}})
    monkeypatch.setattr(agent_task, "patch_ext", lambda *a, **k: {})
    assert agent_task.claim("wo-1") is True
    assert "--expect" in seen["args"] and "pending" in seen["args"]


def test_only_one_of_two_racing_ticks_launches(monkeypatch):
    """The loser sees a state conflict and must launch nothing."""
    won = {"n": 0}

    def fake_rem(*a):
        if a[0] == "transition":
            if won["n"]:
                return {"_err": "ERR_STATE_CONFLICT"}
            won["n"] += 1
            return {"item": {"state": "doing"}}
        return {}
    monkeypatch.setattr(agent_task, "rem", fake_rem)
    monkeypatch.setattr(agent_task, "patch_ext", lambda *a, **k: {})
    launched = []
    monkeypatch.setattr(agent_task, "orders", lambda active_only=True: [_order("wo-1")])
    monkeypatch.setattr(agent_task, "get", lambda i: _order(i))
    monkeypatch.setattr(agent_tick, "launch", lambda it: launched.append(it["id"]) or True)
    monkeypatch.setattr(agent_tick, "reap", lambda items=None, post=True: [])
    a = agent_tick.run(post=False)
    b = agent_tick.run(post=False)
    assert [a["launched"], b["launched"]].count("wo-1") == 1
    assert launched == ["wo-1"]


def test_tick_will_not_launch_while_one_is_running(monkeypatch):
    items = [_order("busy", agent_task.STATE_RUNNING, **{agent_task.EXT_PID: 1,
                                                         agent_task.EXT_PSTART: 2}),
             _order("waiting")]
    monkeypatch.setattr(agent_task, "orders", lambda active_only=True: items)
    monkeypatch.setattr(agent_task, "is_live", lambda p, s: True)
    monkeypatch.setattr(agent_tick, "reap", lambda items=None, post=True: [])
    monkeypatch.setattr(agent_tick, "launch",
                        lambda it: pytest.fail("must stay serial while one order is running"))
    assert agent_tick.run(post=False)["launched"] is None


# --------------------------------------------------------------------------- liveness
@pytest.mark.skipif(not _WINDOWS, reason="process identity probe is the Windows implementation")
def test_liveness_tracks_a_real_process(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000)
    try:
        alive, start = agent_task.proc_identity(p.pid)
        assert alive and start is not None
        assert agent_task.is_live(p.pid, start) is True
        # The pid-reuse case: same number, different process. Must read as dead.
        assert agent_task.is_live(p.pid, start + 1) is False
    finally:
        p.kill()
        p.wait(timeout=10)
    assert agent_task.is_live(p.pid, start) is False, "an exited process must not read as live"


def test_liveness_needs_a_creation_time():
    """Without the discriminator there is nothing to tell a recycled pid from the original, so a
    missing creation time must read as dead rather than as a match."""
    assert agent_task.is_live(1234, None) is False
    assert agent_task.is_live(None, 5) is False


@pytest.mark.skipif(not _WINDOWS, reason="process identity probe is the Windows implementation")
def test_kill_tree_refuses_a_mismatched_identity(monkeypatch):
    killed = []
    monkeypatch.setattr(agent_task.subprocess, "run", lambda *a, **k: killed.append(a))
    assert agent_task.kill_tree(99999999, 12345) is False
    assert killed == [], "a pid whose creation time does not match must never be killed"


def test_reaper_reports_a_dead_run_and_does_not_requeue(monkeypatch):
    item = _order("dead-1", agent_task.STATE_RUNNING,
                  **{agent_task.EXT_PID: 777, agent_task.EXT_PSTART: 5})
    finished, posts = [], []
    monkeypatch.setattr(agent_task, "is_live", lambda p, s: False)
    monkeypatch.setattr(agent_task, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(agent_task, "finish",
                        lambda i, ok, note="", **k: finished.append((i, ok, note)) or {})
    monkeypatch.setattr(agent_tick, "log_tail", lambda it, lines=18: "boom")
    monkeypatch.setattr(agent_tick, "_post", lambda s, t: posts.append(t))
    assert agent_tick.reap([item]) == ["dead-1"]
    assert finished == [("dead-1", False, "runner process died (pid=777)")]
    assert "没有完成" in posts[0] and "不会自动重排" in posts[0]


def test_reaper_leaves_a_just_claimed_order_alone(monkeypatch):
    """Claim and spawn cannot be atomic; a pid-less order inside the grace window is starting."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item = _order("fresh", agent_task.STATE_RUNNING)
    item["updated_at"] = now
    monkeypatch.setattr(agent_task, "finish", lambda *a, **k: pytest.fail("reaped a starting run"))
    assert agent_tick.reap([item], post=False) == []


# --------------------------------------------------------------------------- stall detection
def test_signature_is_stable_and_discriminating(tmp_path):
    ws = str(tmp_path)
    a = agent_task.signature(1, "assertion failed: still enabled", [], ws)
    assert a == agent_task.signature(1, "assertion failed: still enabled", [], ws)
    assert a != agent_task.signature(0, "assertion failed: still enabled", [], ws)
    assert a != agent_task.signature(1, "assertion failed: now disabled", [], ws)


def test_signature_ignores_churn_that_means_nothing(tmp_path):
    ws = str(tmp_path)
    a = agent_task.signature(1, "failed at 2026-08-06T01:02:03Z after 1421ms", [], ws)
    b = agent_task.signature(1, "failed at 2026-08-06T09:44:11Z after 987ms", [], ws)
    assert a == b, "a fresh timestamp every round is not progress"
    c = agent_task.signature(1, "3 checks failed", [], ws)
    d = agent_task.signature(1, "1 checks failed", [], ws)
    assert c != d, "single digits must stay meaningful"


def test_signature_hashes_file_content_not_just_names(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("one", encoding="utf-8")
    a = agent_task.signature(1, "same", ["x.py"], str(tmp_path))
    assert a == agent_task.signature(1, "same", ["x.py"], str(tmp_path))
    f.write_text("two", encoding="utf-8")
    assert a != agent_task.signature(1, "same", ["x.py"], str(tmp_path)), \
        "rewriting a file with different bytes is progress and must change the signature"


# --------------------------------------------------------------------------- rotation
def test_rotation_prompt_drops_the_failed_reasoning():
    failure = "SECRETMARKER the previous approach kept patching the wrong file"
    stale = agent_run.act_prompt("修好那个 bug", "C:\\work", last_failure=failure)
    fresh = agent_run.act_prompt("修好那个 bug", "C:\\work", last_failure=failure, fresh=True)
    assert "SECRETMARKER" in stale
    assert "SECRETMARKER" not in fresh, "a rotation that inherits the failed reasoning is not a rotation"
    assert "修好那个 bug" in fresh, "the problem statement must survive the rotation"
    assert "框定" in fresh, "a rotation must invite re-framing the problem"


def test_act_prompt_forbids_a_check_that_cannot_fail():
    p = agent_run.act_prompt("do it", "C:\\work")
    assert "非零退出" in p and "echo" in p


def test_review_prompt_asks_about_scope_not_just_satisfaction():
    """From a live run: the agent was asked to remove a hardcoded default and also deleted an
    unrelated lookup table. Its check passed, and a reviewer asked only whether the request was
    satisfied said DONE. Collateral damage is a separate question and has to be asked as one."""
    p = agent_run.review_prompt("req", "sum", ["a.py"], "git", "cmd", 0, "out")
    assert "请求之外" in p and "CONTINUE" in p


def test_review_prompt_shows_the_real_output_and_its_provenance():
    p = agent_run.review_prompt("req", "sum", ["a.py"], "self-reported", "the-cmd", 3, "REALOUT")
    assert "REALOUT" in p and "the-cmd" in p and "3" in p
    assert "self-reported" in p, "a self-reported change list is weaker evidence and must say so"


# --------------------------------------------------------------------------- the JSON tail
def test_parse_tail_prefers_the_last_object():
    text = ('例子: {"verify": "echo hi", "summary": "示例"}\n'
            '干完了。\n{"verify": "schtasks /query /tn X | findstr Disabled", '
            '"changed": ["a.py"], "summary": "禁用了"}')
    got = agent_run.parse_tail(text)
    assert got["summary"] == "禁用了" and "schtasks" in got["verify"]


def test_parse_tail_missing_is_empty_not_an_error():
    assert agent_run.parse_tail("我做完了，没给 json") == {}
    assert agent_run.parse_tail("") == {}


def test_parse_tail_accepts_a_null_verify():
    got = agent_run.parse_tail('{"verify": null, "summary": "无法用命令验证"}')
    assert got["verify"] is None and got["summary"]


# --------------------------------------------------------------------------- ext stays small
def test_enqueued_ext_stays_well_inside_the_argv_ceiling(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(agent_task, "rem",
                        lambda *a: captured.update(args=a) or {"item": {"id": "wo-1", "ext": {}}})
    monkeypatch.setattr(agent_task, "runs_root", lambda: str(tmp_path))
    monkeypatch.setattr(agent_task, "append_event", lambda *a, **k: None)
    agent_task.enqueue("crypto", "x" * 8000, workspace=str(tmp_path))
    ext = captured["args"][captured["args"].index("--ext") + 1]
    assert len(ext) < agent_task.EXT_MAX_CHARS, \
        "ext reaches reminder.py as a process argument; unbounded text belongs in the run directory"
    assert "x" * 100 not in ext, "the verbatim request must not be copied into ext"


def test_enqueue_writes_the_request_to_the_run_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_task, "rem", lambda *a: {"item": {"id": "wo-7", "ext": {}}})
    monkeypatch.setattr(agent_task, "runs_root", lambda: str(tmp_path))
    item = agent_task.enqueue("crypto", "完整的请求原文", workspace=str(tmp_path))
    assert agent_task.read_request(item) == "完整的请求原文"


def test_bad_workspace_falls_back_and_says_so():
    ws, note = agent_task.resolve_workspace("Z:\\nope\\definitely-not-here")
    assert ws == agent_task.default_workspace() and note and "not a directory" in note


# --------------------------------------------------------------------------- honesty of reports
def _report(**over):
    kw = dict(short="abc12345", request="停掉发图", summary="改了默认值",
              changed=["tools/gradient_bot.py"], changed_via="git",
              cmd="python -c \"import sys; sys.exit(0)\"", rc=0,
              out="REALVERIFYOUTPUT ok", rprov="cc", decision="DONE 确认已停",
              approach=0, rnd=2, rundir="R")
    kw.update(over)
    return agent_run._done_report(**kw)


def test_done_report_carries_the_real_check_output():
    r = _report()
    assert "REALVERIFYOUTPUT" in r and "rc" not in r.split("验证")[0]
    assert "cc" in r and "DONE" in r


def test_done_report_never_says_handled():
    """The exact word that papered over four days of the incident."""
    for r in (_report(), _report(cmd=None, rc=None, out="")):
        assert "已处理" not in r


def test_done_report_announces_a_missing_check():
    r = _report(cmd=None, rc=None, out="")
    assert "没有可执行验证" in r
    assert "REALVERIFYOUTPUT" not in r


# --------------------------------------------------------------------------- owner gate
def test_text_replies_are_owner_only():
    assert ingest._is_user({"author": {"bot": False, "id": "OWNER"}}, "OWNER") is True
    assert ingest._is_user({"author": {"bot": False, "id": "SOMEONE"}}, "OWNER") is False
    assert ingest._is_user({"author": {"bot": True, "id": "OWNER"}}, "OWNER") is False
    assert ingest._is_user({"author": {"id": "OWNER"}, "webhook_id": "1"}, "OWNER") is False


def test_poll_refuses_to_run_without_a_known_owner(monkeypatch):
    """Fail closed: an unset owner must stop the poll, not widen it to the whole channel."""
    reg = {"streams": {"crypto": {"channel_id": "1"}}, "reader": {"bot_token": "t"}}
    monkeypatch.setattr(ingest, "_fetch", lambda *a, **k: pytest.fail("polled without an owner"))
    with pytest.raises(RuntimeError) as e:
        ingest.poll_all(reg)
    assert "owner" in str(e.value)


# --------------------------------------------------------------------------- NEGATIVE CONTROL
class _Harness:
    """Drives _run_approach with every boundary stubbed, so the loop's own decisions are what is
    under test."""

    def __init__(self, monkeypatch, tmp_path, verify_results, answers=None):
        self.finished, self.verifies, self.reviews = [], [], []
        self.verify_results = list(verify_results)
        self.answers = answers
        item = {"id": "wo-1", "title": "t", "ext": {agent_task.EXT_STREAM: "crypto"}}
        monkeypatch.setattr(agent_task, "get", lambda i: item)
        monkeypatch.setattr(agent_task, "run_dir", lambda it, create=False: str(tmp_path))
        monkeypatch.setattr(agent_task, "patch_ext", lambda *a, **k: {})
        monkeypatch.setattr(agent_task, "set_progress", lambda *a, **k: {})
        monkeypatch.setattr(agent_task, "append_event", lambda *a, **k: None)
        monkeypatch.setattr(agent_task, "finish",
                            lambda i, ok, note="", **k: self.finished.append((i, ok, note)) or {})
        monkeypatch.setattr(agent_run, "detect_changes", lambda ws, claimed: ([], "git"))
        monkeypatch.setattr(agent_run, "post", lambda s, t: None)
        monkeypatch.setattr(agent_run, "_llm", self._llm)
        monkeypatch.setattr(agent_run, "run_verify", self._verify)
        monkeypatch.setattr(agent_run, "STALL_ROUNDS", 2)

    def _llm(self, prompt, chain, timeout, mode):
        if mode == "judge":
            self.reviews.append(prompt)
            return "DONE 看起来没问题", "cc", None
        n = len(self.verifies)
        if self.answers:
            return self.answers[min(n, len(self.answers) - 1)], "codex", None
        return '干完了 {"verify": "check.cmd", "changed": [], "summary": "做了"}', "codex", None

    def _verify(self, cmd, workspace):
        self.verifies.append(cmd)
        i = min(len(self.verifies) - 1, len(self.verify_results) - 1)
        return self.verify_results[i]


def test_a_failing_check_never_closes_the_round(monkeypatch, tmp_path):
    """NEGATIVE CONTROL. The check is poisoned to exit non-zero forever. The order must NOT be
    reported done, and the reviewer must never even be consulted, because review only happens after
    verification passes. Without this test, every other test here would still pass if the return
    code were ignored entirely."""
    h = _Harness(monkeypatch, tmp_path, verify_results=[(1, "assertion failed: still enabled")])
    out = agent_run._run_approach("wo-1", "crypto", "停掉它", str(tmp_path), 0, ["codex"], False)
    assert out["outcome"] == "stalled"
    assert h.finished == [], "a poisoned check must not produce a terminal success"
    assert h.reviews == [], "review must not run on top of a failed check"
    assert len(h.verifies) >= 2, "it must actually keep trying before declaring no progress"


def test_a_passing_check_plus_review_closes_the_round(monkeypatch, tmp_path):
    """The positive control for the test above: same harness, healthy check, must close."""
    h = _Harness(monkeypatch, tmp_path, verify_results=[(0, "disabled ok")])
    out = agent_run._run_approach("wo-1", "crypto", "停掉它", str(tmp_path), 0, ["codex"], False)
    assert out["outcome"] == "done"
    assert h.finished and h.finished[0][1] is True
    assert h.reviews, "a closed round must have been independently reviewed"


def test_a_continue_verdict_blocks_a_passing_check(monkeypatch, tmp_path):
    """A green check that checked the wrong thing must not be enough."""
    h = _Harness(monkeypatch, tmp_path, verify_results=[(0, "ok")])
    monkeypatch.setattr(agent_run, "_llm",
                        lambda p, c, t, mode: ("CONTINUE: 这条命令没验到点子上", "cc", None)
                        if mode == "judge"
                        else ('{"verify": "c", "changed": [], "summary": "s"}', "codex", None))
    out = agent_run._run_approach("wo-1", "crypto", "停掉它", str(tmp_path), 0, ["codex"], False)
    assert out["outcome"] == "stalled"
    assert h.finished == [], "a CONTINUE verdict must not close the order"


def test_an_unavailable_provider_ends_the_approach_not_the_order(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path, verify_results=[(0, "ok")])
    monkeypatch.setattr(agent_run, "_llm", lambda p, c, t, mode: ("", None, "codex not found"))
    out = agent_run._run_approach("wo-1", "crypto", "x", str(tmp_path), 0, ["codex"], False)
    assert out["outcome"] == "stalled"
    assert h.finished == []


def test_llm_rejects_a_typo_mode():
    """llmcall's own mode tuple is dead code, so a typo would silently degrade an agentic call to a
    read-only judgement that changes nothing and then reports success."""
    with pytest.raises(ValueError):
        agent_run._llm("p", ["codex"], 10, "agentic")


def test_unrunnable_check_counts_as_a_failure(tmp_path):
    rc, out = agent_run.run_verify("definitely-not-a-real-command-xyz", str(tmp_path))
    assert rc != 0, "a check that cannot run has not passed"


# --------------------------------------------------------------------------- the check runner
# The most dangerous function here. Every honesty guarantee in this tier reduces to "run_verify
# returns nonzero when the job is not done"; a runner that quietly returns 0 turns the whole design
# back into the thing it replaced. Each case below is a real behaviour that was measured, not
# assumed, and two of them were wrong in the first implementation.
@pytest.mark.skipif(not _WINDOWS, reason="the check runner uses PowerShell on Windows")
class TestRunVerify:

    def test_native_exit_code_survives_exactly(self, tmp_path):
        """`powershell -Command` collapses any native nonzero to 1; the real code must not."""
        rc, _ = agent_run.run_verify('python -c "import sys; sys.exit(3)"', str(tmp_path))
        assert rc == 3

    def test_success_is_zero(self, tmp_path):
        rc, _ = agent_run.run_verify('python -c "import sys; sys.exit(0)"', str(tmp_path))
        assert rc == 0

    def test_noisy_but_successful_command_still_passes(self, tmp_path):
        rc, out = agent_run.run_verify(
            'python -c "import sys; sys.stderr.write(\'warning: noisy\'); sys.exit(0)"',
            str(tmp_path))
        assert rc == 0 and "noisy" in out

    def test_a_check_that_captures_stderr_still_passes(self, tmp_path):
        """The redirected form is where the Continue preference earns its place, and checks reach
        for it constantly (`... 2>&1 | Select-String ...`). Measured: under Stop this exact command
        returns 1 for a program that exited 0, so a correct fix would be rejected forever. Bare
        native stderr is NOT enough to show this; PowerShell only turns it into an error record once
        it is redirected, which is why the plainer test above cannot catch a Stop regression."""
        rc, _ = agent_run.run_verify(
            '$o = python -c "import sys; sys.stderr.write(\'warn\'); sys.exit(0)" 2>&1; '
            'exit $LASTEXITCODE', str(tmp_path))
        assert rc == 0

    def test_a_cmdlet_failure_is_not_reported_as_success(self, tmp_path):
        """THE dangerous case. Re-raising only $LASTEXITCODE returns 0 here, so a check that failed
        would read as a check that passed, which is precisely the disease this tier treats."""
        rc, _ = agent_run.run_verify("Get-ChildItem NoSuchPathXYZ", str(tmp_path))
        assert rc != 0

    def test_powershell_syntax_is_accepted(self, tmp_path):
        """The first live run produced exactly this shape and cmd answered
        "& was unexpected at this time.", recording a correct fix as a failure."""
        rc, _ = agent_run.run_verify("& { if ($true) { exit 0 } else { exit 1 } }", str(tmp_path))
        assert rc == 0

    def test_nested_quotes_survive(self, tmp_path):
        """Real checks are full of them, which is why the command goes through a file and not argv."""
        rc, out = agent_run.run_verify(
            'python -c "import sys; print(\'ok\' if \'a\' in [\'a\'] else \'no\'); sys.exit(0)"',
            str(tmp_path))
        assert rc == 0 and "ok" in out

    def test_a_bash_ism_fails_loudly(self, tmp_path):
        """PowerShell 5.1 has no && . Failing is the safe direction; passing would not be."""
        rc, _ = agent_run.run_verify("echo a && echo b", str(tmp_path))
        assert rc != 0

    def test_chinese_output_is_not_mojibake(self, tmp_path):
        """The check output is quoted verbatim into the channel report, so a console-codepage
        round trip would deliver garbage as evidence."""
        rc, out = agent_run.run_verify("Write-Output '断言失败:任务仍启用'", str(tmp_path))
        assert rc == 0 and out == "断言失败:任务仍启用"

    def test_an_explicit_exit_wins_over_a_swallowed_error(self, tmp_path):
        """Documented precedence, pinned so it stays a decision rather than a surprise: a check that
        catches its own error and then asserts a verdict is believed."""
        rc, _ = agent_run.run_verify(
            "try { throw 'internal' } catch { }; exit 0", str(tmp_path))
        assert rc == 0

    def test_a_swallowed_error_without_a_verdict_is_a_failure(self, tmp_path):
        """The other half of the same rule. A caught terminating error still lands in $Error, so a
        check that neither exits nor propagates is judged failed. Safe direction: it costs a round
        and can never manufacture a success."""
        rc, _ = agent_run.run_verify(
            "try { throw 'internal' } catch { }; Write-Output done", str(tmp_path))
        assert rc != 0

    def test_it_runs_in_the_given_workspace(self, tmp_path):
        (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
        rc, _ = agent_run.run_verify(
            'python -c "import os,sys; sys.exit(0 if os.path.isfile(\'marker.txt\') else 1)"',
            str(tmp_path))
        assert rc == 0


def test_the_prompt_names_the_shell():
    p = agent_run.act_prompt("do it", "C:\\work")
    assert agent_run.VERIFY_SHELL in p
    if _WINDOWS:
        assert "&&" in p, "the prompt must warn that PowerShell 5.1 has no && "


def test_report_is_chunked_below_the_discord_limit(monkeypatch):
    sent = []
    monkeypatch.setattr(agent_run.relay, "relay", lambda s, t: sent.append(t) or True)
    agent_run.post("crypto", "x" * 5000)
    assert len(sent) == 3 and all(len(s) <= agent_run._DISCORD_MAX for s in sent)


def test_runner_points_llmcall_at_the_shim():
    """Left at its machine default, the delegate retries cc then CODEX then claude, so the cc leg of
    an agentic call runs codex a second time and every edit happens twice."""
    assert os.environ.get("LLMCALL_AGENT_RUNNER") == agent_run._SHIM
    assert agent_run.APPROACH_CHAINS[0] == ["codex"], "the acting chain must be a single provider"
    assert agent_run.REVIEW_CHAIN[0] != agent_run.APPROACH_CHAINS[0][0], \
        "the reviewer must not be the actor"
