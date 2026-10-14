"""Synthetic regressions for retained T14 cleanup traces; no model or live pool."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_run
import agent_task
import agent_tick
import store
from make_fixtures import llm_call_result
from test_agent_completion import real_process, load_process

process = load_process()


def Result(**fields):
    fields.setdefault('cleanup_confirmed', None)
    return llm_call_result(**fields)


@pytest.fixture
def work(tmp_path, monkeypatch, real_process):
    monkeypatch.chdir(tmp_path)
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("SCHEDULE_DB_PATH", str(tmp_path / "synthetic.sqlite3"))
    monkeypatch.setenv("AGENT_CENTER_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    if sys.platform != "win32":
        # The production identity probe is Windows-only; portable tests own this synthetic runner.
        runner_pid = os.getpid()
        monkeypatch.setattr(agent_task, "proc_identity",
                            lambda pid: (True, 456) if pid == runner_pid else (False, None))
    monkeypatch.setattr(agent_run, "post", lambda *a: pytest.fail("notification"))
    monkeypatch.setattr(agent_tick, "_post", lambda *a: pytest.fail("notification"))
    store.init_db()
    item = store.add_item("SYNTHETIC_WORK", source=agent_task.WORK_SOURCE,
        ext={agent_task.EXT_STATE: "queued", agent_task.EXT_WORKSPACE: str(workspace)})
    Path(agent_task.run_dir(item)).mkdir(parents=True)
    Path(agent_task.run_dir(item), "request.txt").write_text(
        "Create fix.txt and preserve all existing user files.", encoding="utf-8")
    assert agent_task.claim(item["id"])
    assert agent_task.begin_spawn(item["id"], 1)
    monkeypatch.setattr(agent_run, "run_verify", lambda *a, **k: (0, "synthetic passed"))
    monkeypatch.setattr(agent_run, "_llm", lambda p, t, mode: reply(mode))
    return item["id"], workspace


def reply(mode, **kwargs):
    assert mode == "agent", "the runner calls only the executor"
    return Result(text='{"verify":"synthetic-check","summary":"synthetic fix","changed":[]}',
                  effective_model="synthetic-" + mode, model_family="family-" + mode,
                  execution_started=True, outcome="success", cleanup_confirmed=True, **kwargs)


def assert_reserved(iid):
    op = agent_task.operation(iid)
    assert op["released_at"] is None, "uncertain descendants must retain the writer reservation"
    assert not agent_task.release(iid, op["generation"]), "direct release must also fail closed"
    other = store.add_item("SYNTHETIC_NEXT_WORK", source=agent_task.WORK_SOURCE,
                           ext={agent_task.EXT_STATE: "queued"})
    assert not agent_task.claim(other["id"])


@pytest.mark.parametrize("phase", ["actor", "verify"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_cleanup_failure_retains_reservation_across_all_boundaries(work, monkeypatch, phase, cancelled):
    iid, workspace = work
    failure = process.ProcessOutput(None, "synthetic cleanup failed", True, "cleanup_failed")
    def fail_process(*args, **kwargs):
        if cancelled:
            agent_task.cancel(iid, "synthetic cancel")
        return failure
    monkeypatch.setattr(process, "run", fail_process)
    if phase == "verify":
        monkeypatch.setattr(agent_run, "run_verify", lambda *a, **k:
            agent_run._contained_command([], str(workspace), 1))
    else:
        def llm(prompt, timeout, mode):
            fail_process()
            return Result(error="synthetic cleanup failed", outcome="cleanup_failed",
                          execution_started=True)
        monkeypatch.setattr(agent_run, "_llm", llm)
    try:
        agent_run.run_order(iid, post_reports=False, generation=1)
    except RuntimeError:
        pass  # A cleanup failure may surface visibly; it still must retain the slot.
    assert_reserved(iid)
    assert agent_task.operation(iid)["outcome"] == ("cancelled" if cancelled else "reconcile")


def test_parent_exit_cannot_release_cleanup_uncertainty(work, monkeypatch):
    iid, workspace = work
    monkeypatch.setattr(process, "run", lambda *a, **k:
        process.ProcessOutput(None, "synthetic cleanup failed", True, "cleanup_failed"))
    monkeypatch.setattr(agent_run, "run_verify", lambda *a, **k:
        agent_run._contained_command([], str(workspace), 1))
    agent_run.run_order(iid, post_reports=False, generation=1)
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (False, None))
    store.init_db()  # Reopen/migration must retain the durable uncertainty.
    agent_tick.reap(post=False)
    agent_tick.reap(post=False)
    assert_reserved(iid)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job cleanup trace")
def test_windows_cleanup_failure_keeps_slot_while_descendant_writes(work, monkeypatch):
    iid, workspace = work
    heartbeat = workspace / "heartbeat.txt"
    child_code = ("import pathlib,time; p=pathlib.Path(" + repr(str(heartbeat)) + "); "
                  "end=time.monotonic()+12\nwhile time.monotonic()<end:\n"
                  " p.write_text(str(time.monotonic())); time.sleep(.04)")
    parent_code = ("import subprocess,sys; subprocess.Popen([sys.executable,'-c'," +
                   repr(child_code) + "],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
                   "stderr=subprocess.DEVNULL,creationflags=0x08000000); print('parent exited')")
    jobs, outputs = [], []
    real_close, real_run = process.WindowsJob.close, process.run
    def fail_close(job):
        jobs.append(job)
        raise OSError("synthetic Job close failure")
    def run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        outputs.append(result)
        return result
    monkeypatch.setattr(process.WindowsJob, "close", fail_close)
    monkeypatch.setattr(process, "run", run)
    monkeypatch.setattr(agent_run, "run_verify", lambda *a, **k:
        agent_run._contained_command([sys.executable, "-c", parent_code], str(workspace), 3))
    try:
        agent_run.run_order(iid, post_reports=False, generation=1)
        deadline = time.monotonic() + 2
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        first = heartbeat.read_text()
        time.sleep(.16)
        assert first != heartbeat.read_text(), "probe must demonstrate a live descendant"
        assert outputs[0].outcome == "cleanup_failed" and outputs[0].returncode is None
        assert_reserved(iid)
    finally:
        for job in jobs:
            real_close(job)


def test_interrupted_child_receipt_cannot_be_cleared_by_parent_exit(work, monkeypatch):
    iid, workspace = work
    def interrupted(*args):
        raise KeyboardInterrupt("synthetic lost child receipt")
    monkeypatch.setattr(agent_run, "_llm", interrupted)
    with pytest.raises(KeyboardInterrupt):
        agent_run.run_order(iid, post_reports=False, generation=1)
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (False, None))
    agent_tick.reap(post=False)
    assert_reserved(iid)


@pytest.mark.parametrize("outcome", ["cancelled", "timeout", "failed"])
def test_started_model_failure_without_cleanup_confirmation_stays_unknown(work, monkeypatch, outcome):
    iid, workspace = work
    def llm(prompt, timeout, mode):
        if outcome == "cancelled":
            agent_task.cancel(iid, "synthetic cancellation")
        return Result(error="synthetic interrupted model", outcome=outcome, execution_started=True)
    monkeypatch.setattr(agent_run, "_llm", llm)
    agent_run.run_order(iid, post_reports=False, generation=1)
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (False, None))
    agent_tick.reap(post=False)
    assert_reserved(iid)


def test_cleanup_receipt_write_failure_holds_reservation_after_reaping(work, monkeypatch):
    iid, workspace = work
    append = store._append_event
    def fail_receipt(conn, item_id, actor, event, *args, **kwargs):
        if event == "work_child_finish":
            raise OSError("synthetic receipt persistence failure")
        return append(conn, item_id, actor, event, *args, **kwargs)
    monkeypatch.setattr(store, "_append_event", fail_receipt)
    with pytest.raises(OSError, match="receipt persistence"):
        agent_run.run_order(iid, post_reports=False, generation=1)
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (False, None))
    agent_tick.reap(post=False)
    assert_reserved(iid)


@pytest.mark.parametrize("cancelled", [False, True])
def test_confirmed_command_cleanup_allows_release_even_after_interruption(work, monkeypatch, cancelled):
    iid, workspace = work
    def run(*args, **kwargs):
        if cancelled:
            agent_task.cancel(iid, "synthetic cancellation")
        # Shared ProcessOutput distinguishes cleaned interruption from cleanup_failed.
        outcome = "cancelled" if cancelled else "timeout"
        return process.ProcessOutput(None, outcome, True, outcome, cleanup_confirmed=True)
    monkeypatch.setattr(process, "run", run)
    monkeypatch.setattr(agent_run, "run_verify", lambda *a, **k:
        agent_run._contained_command([], str(workspace), 1))
    agent_run.run_order(iid, post_reports=False, generation=1)
    op = agent_task.operation(iid)
    assert op["released_at"] and op["cleanup_state"] == "quiescent"
    assert op["outcome"] == ("cancelled" if cancelled else "reconcile")


def test_record_fsync_failure_cannot_finish_work(work, monkeypatch):
    iid, workspace = work
    fsync = os.fsync
    calls = []
    def fail_record(fd):
        calls.append(fd)
        if len(calls) == 2:  # prompt durable, actor output cannot be persisted
            raise OSError("synthetic record fsync failure")
        return fsync(fd)
    monkeypatch.setattr(os, "fsync", fail_record)
    with pytest.raises(OSError, match="record fsync"):
        agent_run.run_order(iid, post_reports=False, generation=1)
    assert agent_task.operation(iid)["outcome"] == "reconcile"


def test_v2_started_reservation_migrates_to_unknown_without_changing_public_rows(work):
    import sqlite3
    iid, workspace = work
    assert agent_task.start_runner(iid, 1, 123, 456)
    before, events = agent_task.get(iid), store.get_events(iid)
    with sqlite3.connect(os.environ["SCHEDULE_DB_PATH"]) as conn:
        for column in ("cleanup_state", "cleanup_receipt", "baseline", "baseline_sha256", "baseline_workspace"):
            conn.execute("ALTER TABLE agent_operations DROP COLUMN " + column)
        conn.execute("PRAGMA user_version=2")
    store.init_db()
    operation = agent_task.operation(iid)
    assert operation["cleanup_state"] == "unknown"
    assert agent_task.get(iid) == before and store.get_events(iid) == events
    store.init_db()
    assert agent_task.operation(iid) == operation
    assert_reserved(iid)
