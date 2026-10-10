"""A verified job kill settles llmcall's unconfirmed cleanup; what it cannot vouch for stays held.

Real Windows processes started by this file only (the synthetic runner of test_runner_job and a
plain sleeping runner), each terminated through a handle held on it. The same file runs against
code without these rules (the negative control): there an 'unknown' child cleanup holds the slot
after a verified job kill, an unchecked breakaway report reads as "none", and a runner that was
ended while still suspended leaves its order blocked as "launch outcome unknown".
"""
import json
import subprocess
import sys

import pytest

import agent_task
import agent_tick
import runner_job
import store
from test_agent_lifecycle import pool, started_order  # noqa: F401  (fixture reuse)
from test_runner_job import SLEEP, held, runner  # noqa: F401  (fixture reuse)
from test_work_actions import case, module as work_module, request as work_request  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")

UNCONFIRMED = {"phase": "actor", "outcome": "cancelled", "cleanup_confirmed": False,
               "returncode": None, "attempts": []}


def leave_cleanup_unknown(iid, receipt=None):
    """What agent_run commits when llmcall reports that it could not confirm its own cleanup."""
    assert agent_task.child_finished(iid, 1, dict(receipt or UNCONFIRMED), quiescent=False)
    assert agent_task.operation(iid)["cleanup_state"] == "unknown"


def next_order_claims():
    queued = store.add_item("Synthetic next order", source=agent_task.WORK_SOURCE,
                            ext={agent_task.EXT_STATE: "queued"})
    return bool(agent_task.claim(queued["id"]))


def events(iid):
    return [json.loads(line) for line in open(agent_task.run_dir(agent_task.get(iid)) + "/events.jsonl",
                                              encoding="utf-8")]


def test_job_kill_settles_cleanup_that_llmcall_left_unknown(runner, held, pool):  # noqa: F811
    iid, _pids = runner("nested")
    leave_cleanup_unknown(iid)
    result = agent_tick.stop(iid, post=False, expected_generation=1)[0]
    for label in ("runner", "child", "grandchild", "nested"):
        assert held.wait_gone(label), label + " survived the stop"
    assert result["cleanup"] == "released", "a verified job kill must supersede the unknown report"
    op = agent_task.operation(iid)
    assert op["released_at"] and op["cleanup_state"] == "quiescent"
    receipt = json.loads(op["cleanup_receipt"])
    assert receipt["authority"] == "verified-job-kill" and receipt["breakaway_check"] == "checked"
    assert receipt["previous_receipt"]["cleanup_confirmed"] is False   # the report is kept
    assert next_order_claims()


def test_unknown_cleanup_whose_child_receipt_names_a_breakaway_stays_held(runner, held, pool):  # noqa: F811
    iid, _pids = runner()
    leave_cleanup_unknown(iid, {**UNCONFIRMED, "attempts": [{"outcome": "cancelled", "broke_away": [4099]}]})
    result = agent_tick.stop(iid, post=False, expected_generation=1)[0]
    assert held.wait_gone("runner") and held.wait_gone("grandchild")
    assert result["cleanup"] == "held"
    assert agent_task.operation(iid)["released_at"] is None
    assert not next_order_claims()


def test_unknown_cleanup_with_a_breakaway_found_by_the_stop_stays_held(runner, held, pool):  # noqa: F811
    iid, pids = runner("breakaway")
    if "breakaway_error" in pids:
        pytest.skip("this test process sits in a job that forbids breakaway: " + pids["breakaway_error"])
    leave_cleanup_unknown(iid)
    result = agent_tick.stop(iid, post=False, expected_generation=1)[0]
    assert result.get("broke_away") == [pids["breakaway"]] and held.alive("breakaway")
    assert result["cleanup"] == "held"
    assert agent_task.operation(iid)["released_at"] is None


def test_only_a_job_kill_supersedes_unknown_cleanup_never_the_tree_fallback(pool):  # noqa: F811
    # Store level and deterministic: a real-OS tree fallback also holds on any unrelated orphan
    # born during the stop, which would let this test pass for the wrong reason.
    iid = started_order(pool)
    assert agent_task.child_started(iid, 1, {"phase": "actor"})
    leave_cleanup_unknown(iid)
    assert not agent_task.cancel(iid, expected_generation=1).get("_err")
    op = agent_task.operation(iid)
    proof = {"roots": [[123, "456"]], "members": [[123, "456"]], "confirmed_at": "2026-01-01T00:00:00+00:00",
             "broke_away": [], "breakaway_check": "checked"}
    assert not agent_task.release_verified_cleanup(iid, 1, {"authority": "verified-tree-kill", **proof}, op)
    assert agent_task.operation(iid)["cleanup_state"] == "unknown"
    assert agent_task.release_verified_cleanup(iid, 1, {"authority": "verified-job-kill", **proof}, op)
    assert agent_task.operation(iid)["released_at"] is not None


def unreadable_breakaways(monkeypatch):
    def fail(self, table):
        raise OSError("synthetic: member identity unreadable")
    monkeypatch.setattr(runner_job.RunnerJob, "_breakaways", fail)


def test_an_unchecked_breakaway_report_is_said_not_read_as_none(runner, held, monkeypatch, pool):  # noqa: F811
    iid, _pids = runner()
    unreadable_breakaways(monkeypatch)
    result = agent_tick.stop(iid, post=False, expected_generation=1)[0]
    assert held.wait_gone("runner") and held.wait_gone("grandchild")
    # The job count still proves the kill, so the slot is released; the reply says what is unknown.
    assert result["cleanup"] == "released"
    assert str(result.get("breakaway_check", "")).startswith("failed: OSError"), result
    receipt = json.loads(agent_task.operation(iid)["cleanup_receipt"])
    assert receipt["breakaway_check"].startswith("failed: OSError")
    assert any(row.get("event") == "runner_breakaway_unchecked" for row in events(iid))


def test_an_unchecked_breakaway_report_keeps_unknown_cleanup_held(runner, held, monkeypatch, pool):  # noqa: F811
    iid, _pids = runner()
    leave_cleanup_unknown(iid)
    unreadable_breakaways(monkeypatch)
    result = agent_tick.stop(iid, post=False, expected_generation=1)[0]
    assert held.wait_gone("runner")
    assert result["cleanup"] == "held"
    assert str(result.get("breakaway_check", "")).startswith("failed:")


@pytest.fixture
def sleeper(pool, tmp_path, monkeypatch, held):  # noqa: F811
    """A queued order whose runner only sleeps; every runner Popen is held for liveness checks."""
    script = tmp_path / "sleeping_runner.py"
    script.write_text(SLEEP, encoding="utf-8")
    monkeypatch.setattr(agent_tick, "RUNNER", str(script))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    started = []
    original_popen = subprocess.Popen

    class Recorded(original_popen):
        def __init__(self, argv, *args, **kwargs):
            super().__init__(argv, *args, **kwargs)
            if str(script) in [str(value) for value in argv]:
                held.hold("runner%d" % len(started), self.pid)
                started.append(self)
    monkeypatch.setattr(agent_tick.subprocess, "Popen", Recorded)
    item = store.add_item("Synthetic sleeping order", source=agent_task.WORK_SOURCE,
                          ext={agent_task.EXT_STATE: "queued", agent_task.EXT_WORKSPACE: str(workspace)})
    return item["id"], started


def failing_resume(monkeypatch, times, error=None):
    calls, real = [], runner_job.resume

    def resume(pid, jobs=None):
        calls.append(pid)
        if len(calls) <= times:
            raise (error or runner_job.JobUnavailable)("synthetic resume failure")
        return real(pid, jobs)
    monkeypatch.setattr(runner_job, "resume", resume)


def claim_and_launch(iid):
    op = agent_task.claim(iid, return_operation=True)
    assert op, "the order must be claimable"
    return op["generation"], agent_tick.launch(agent_task.get(iid), generation=op["generation"],
                                               post_reports=False)


def test_a_runner_ended_while_suspended_is_not_started_and_is_retried(sleeper, held, monkeypatch):
    iid, started = sleeper
    failing_resume(monkeypatch, times=1)
    generation, launched = claim_and_launch(iid)
    assert launched is False
    assert held.wait_gone("runner0"), "the suspended runner must be ended"
    op = agent_task.operation(iid)
    assert op["outcome"] == "not_started", "nothing ran: this is not an ambiguous launch"
    assert op["released_at"] is not None
    item = agent_task.get(iid)
    assert item["state"] == "pending" and agent_task.exec_state(item) == agent_task.STATE_QUEUED
    # The retry: the next tick claims a new generation and the runner really starts this time.
    generation2, launched2 = claim_and_launch(iid)
    assert generation2 == generation + 1 and launched2 is True
    assert held.alive("runner1")
    stopped = agent_tick.stop(iid, post=False, expected_generation=generation2)[0]
    assert stopped["stopped"] is True and held.wait_gone("runner1")


def test_repeated_not_started_launches_end_blocked_with_the_slot_free(sleeper, held, monkeypatch):
    iid, started = sleeper
    failing_resume(monkeypatch, times=99)
    for attempt in range(3):   # the documented bound (agent_tick.NOT_STARTED_ATTEMPTS)
        _generation, launched = claim_and_launch(iid)
        assert launched is False and held.wait_gone("runner%d" % attempt)
        assert agent_task.operation(iid)["outcome"] == "not_started"
    item = agent_task.get(iid)
    assert item["state"] == "blocked" and "runner not started" in (item.get("block_reason") or "")
    assert agent_task.operation(iid)["released_at"] is not None
    assert next_order_claims()
    assert agent_tick.NOT_STARTED_ATTEMPTS == 3


def test_a_runner_with_a_thread_already_resumed_stays_launch_outcome_unknown(sleeper, held, monkeypatch):
    iid, started = sleeper
    # Resuming failed after a thread had run: killing it now does not prove that nothing started.
    # (getattr keeps this guard runnable on code that predates the class: it then raises the base.)
    failing_resume(monkeypatch, times=1,
                   error=getattr(runner_job, "ResumeIncomplete", runner_job.JobUnavailable))
    _generation, launched = claim_and_launch(iid)
    assert launched is False and held.wait_gone("runner0")
    assert agent_task.operation(iid)["outcome"] == "reconcile"
    assert agent_task.get(iid)["state"] == "blocked"


def test_a_suspended_runner_whose_end_is_unconfirmed_stays_launch_outcome_unknown(sleeper, held, monkeypatch):
    iid, started = sleeper
    failing_resume(monkeypatch, times=1)
    original_kill = subprocess.Popen.kill

    def kill(self):
        raise OSError("synthetic: terminate refused")
    monkeypatch.setattr(subprocess.Popen, "kill", kill)
    _generation, launched = claim_and_launch(iid)
    monkeypatch.setattr(subprocess.Popen, "kill", original_kill)
    assert launched is False
    op = agent_task.operation(iid)
    assert op["outcome"] == "reconcile"
    assert agent_task.get(iid)["state"] == "blocked"


def test_the_console_stop_reply_says_what_the_stop_could_not_check(case, monkeypatch):  # noqa: F811
    actions = work_module()
    database, workspace, item = case
    action = actions.start(work_request(actions, database, workspace, item),
                           db_path=str(database), workspace_root=str(workspace))['action']
    work_id = action['work_item_id']
    assert agent_task.claim(work_id) and agent_task.begin_spawn(work_id, 1)
    assert agent_task.start_runner(work_id, 1, 123, 456)
    projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    payload = {'item_id': item['id'], 'action_id': action['id'], 'revision': projection['revision'],
               'request_id': 'synthetic-stop-unchecked-breakaway'}

    def stop(item_id, **kwargs):
        # What agent_tick.stop does after a verified job kill whose breakaway check failed.
        assert not agent_task.cancel(item_id, expected_generation=1).get('_err')
        receipt = {'authority': 'verified-job-kill', 'roots': [[123, '456']], 'members': [[123, '456']],
                   'confirmed_at': '2026-01-01T00:00:00+00:00', 'broke_away': [],
                   'breakaway_check': 'failed: OSError: synthetic'}
        assert agent_task.release_verified_cleanup(item_id, 1, receipt, agent_task.operation(item_id))
        return [{'id': item_id, 'stopped': True, 'killed': True, 'status': 'terminated',
                 'cleanup': 'released', 'breakaway_check': 'failed: OSError: synthetic'}]
    monkeypatch.setattr(agent_tick, 'stop', stop)
    reply = actions.stop(payload, db_path=str(database))
    assert reply['status'] == 'stopped'
    assert 'failed: OSError: synthetic' in reply['action']['summary'], reply['action']['summary']


class OneThreadKernel:
    """A kernel32 stand-in for NativeJobs.resume: one runner thread, then a failing enumeration."""

    def __init__(self, owner):
        self.owner, self.resumed = owner, 0

    def CreateToolhelp32Snapshot(self, flags, pid):
        return 7

    def Thread32First(self, snapshot, entry):
        entry._obj.th32OwnerProcessID, entry._obj.th32ThreadID = self.owner, 11
        return True

    def Thread32Next(self, snapshot, entry):
        return False   # with a last error other than ERROR_NO_MORE_FILES: enumeration failed

    def OpenThread(self, access, inherit, thread_id):
        return 9

    def ResumeThread(self, thread):
        self.resumed += 1
        return 1

    def CloseHandle(self, handle):
        return True


@pytest.mark.parametrize("owner, expected", [(4242, "ResumeIncomplete"), (4343, "JobUnavailable")])
def test_a_resume_failure_says_whether_a_runner_thread_already_ran(monkeypatch, owner, expected):
    jobs = runner_job.NativeJobs()
    jobs._k = OneThreadKernel(owner)
    monkeypatch.setattr(runner_job.ctypes, "get_last_error", lambda: 5)
    with pytest.raises(runner_job.JobUnavailable) as caught:
        jobs.resume(4242)
    assert type(caught.value).__name__ == expected
    assert jobs._k.resumed == (1 if owner == 4242 else 0)
