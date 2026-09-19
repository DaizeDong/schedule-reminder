"""Synthetic operation fixtures; never open the user's reminder database."""
import concurrent.futures
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_task
import agent_tick
import store


@pytest.fixture
def pool(tmp_path, monkeypatch):
    path = str(tmp_path / "synthetic.sqlite3")
    monkeypatch.setenv("SCHEDULE_DB_PATH", path)
    monkeypatch.setenv("AGENT_CENTER_RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent_tick, "_post", lambda *a: pytest.fail("notification"))
    store.init_db(path)
    return path


def make_order(pool, name="FAKE_CANARY_WORK"):
    return store.add_item(name, source=agent_task.WORK_SOURCE,
                          ext={agent_task.EXT_STATE: "queued"}, db_path=pool)


def test_claim_state_ext_and_generation_are_atomic(pool):
    item = make_order(pool)
    assert agent_task.claim(item["id"]) is True
    current = store.get_item(item["id"], db_path=pool)
    op = agent_task.operation(item["id"])
    assert current["state"] == "doing"
    assert current["ext"][agent_task.EXT_STATE] == "running"
    assert current["ext"][agent_task.EXT_GENERATION] == op["generation"]
    assert op["checkpoint"] == "claimed" and op["run_id"] and op["attempt_id"]


def test_two_claims_on_different_orders_still_have_one_writer(pool):
    items = [make_order(pool) for _ in range(2)]
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        won = list(executor.map(agent_task.claim, [it["id"] for it in items]))
    assert sorted(won) == [False, True]


def test_cancel_cannot_be_overwritten_by_receipt_or_finish(pool):
    item = make_order(pool)
    agent_task.claim(item["id"])
    generation = agent_task.operation(item["id"])["generation"]
    agent_task.cancel(item["id"], "synthetic cancel")
    assert not agent_task.record_process(item["id"], 123, 456, generation=generation)
    assert not agent_task.finish(item["id"], True, generation=generation)
    assert store.get_item(item["id"], db_path=pool)["state"] == "cancelled"


def test_runner_claims_generation_before_parent_receipt(pool):
    item = make_order(pool)
    agent_task.claim(item["id"])
    gen = agent_task.operation(item["id"])["generation"]
    assert agent_task.begin_spawn(item["id"], gen)
    assert agent_task.start_runner(item["id"], gen, 123, 456)
    assert not agent_task.start_runner(item["id"], gen, 123, 456)
    assert agent_task.record_process(item["id"], 123, 456, generation=gen)
    assert not agent_task.record_process(item["id"], 123, 789, generation=gen)


def test_migration_rerun_preserves_frozen_item_and_event_readers(pool):
    item = make_order(pool)
    before = store.get_item(item["id"], db_path=pool)
    events = store.get_events(item["id"], db_path=pool)
    store.init_db(pool)
    store.init_db(pool)
    assert store.get_item(item["id"], db_path=pool) == before
    assert store.get_events(item["id"], db_path=pool) == events


def test_claim_rolls_back_state_ext_and_operation_when_event_fails(pool, monkeypatch):
    item = make_order(pool)
    before = agent_task.get(item["id"])
    monkeypatch.setattr(store, "_append_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
    with pytest.raises(RuntimeError):
        agent_task.claim(item["id"])
    assert agent_task.get(item["id"]) == before
    assert agent_task.operation(item["id"]) is None


@pytest.mark.parametrize("spawned", [False, True])
def test_claim_exit_or_spawn_without_receipt_reconciles_and_late_runner_cannot_start(pool, monkeypatch, spawned):
    item = make_order(pool)
    agent_task.claim(item["id"])
    if spawned:
        assert agent_task.begin_spawn(item["id"], 1)
    monkeypatch.setattr(agent_tick, "CLAIM_GRACE_SECONDS", 0)
    assert agent_tick.reap(post=False) == [item["id"]]
    assert agent_task.operation(item["id"])["outcome"] == "reconcile"
    assert not agent_task.begin_spawn(item["id"], 1)
    assert not agent_task.start_runner(item["id"], 1, 123, 456)
    assert not agent_task.claim(item["id"])


def test_reaper_cannot_overwrite_runner_receipt_that_arrives_after_snapshot(pool, monkeypatch):
    item = make_order(pool)
    agent_task.claim(item["id"])
    agent_task.begin_spawn(item["id"], 1)
    original = agent_task.reconcile
    def racing_reconcile(iid, snapshot, note):
        assert agent_task.start_runner(iid, 1, 123, 456)
        return original(iid, snapshot, note)
    monkeypatch.setattr(agent_task, "reconcile", racing_reconcile)
    monkeypatch.setattr(agent_tick, "CLAIM_GRACE_SECONDS", 0)
    assert agent_tick.reap(post=False) == []
    assert agent_task.operation(item["id"])["checkpoint"] == "running"


def test_pid_reuse_reconciles_without_killing_unrelated_process(pool, monkeypatch):
    item = make_order(pool)
    agent_task.claim(item["id"])
    agent_task.begin_spawn(item["id"], 1)
    agent_task.start_runner(item["id"], 1, 123, 456)
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (True, 789))
    monkeypatch.setattr(agent_task, "kill_tree", lambda *a: pytest.fail("unrelated process kill"))
    assert agent_tick.reap(post=False) == [item["id"]]


def test_unqueryable_process_keeps_serial_reservation(pool, monkeypatch):
    item = make_order(pool)
    agent_task.claim(item["id"])
    agent_task.begin_spawn(item["id"], 1)
    agent_task.start_runner(item["id"], 1, 123, 456)
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (True, None))
    assert agent_tick.reap(post=False) == []
    assert not agent_task.claim(make_order(pool)["id"])


def test_stale_completion_cannot_finish_reopened_generation(pool):
    item = make_order(pool)
    iid = item["id"]
    agent_task.claim(iid)
    agent_task.cancel(iid)
    agent_task.release(iid, 1)  # no runner was started
    store.transition(iid, "pending")
    store.update_item(iid, ext={agent_task.EXT_STATE: "queued"})
    assert agent_task.claim(iid)
    assert agent_task.operation(iid)["generation"] == 2
    assert not agent_task.finish(iid, True, generation=1)
    assert not agent_task.record_process(iid, 123, 456, generation=1)
    assert agent_task.get(iid)["state"] == "doing"


def test_cancelled_running_generation_holds_writer_until_process_quiescent(pool, monkeypatch):
    iid = make_order(pool)["id"]
    other = make_order(pool)["id"]
    agent_task.claim(iid)
    agent_task.begin_spawn(iid, 1)
    agent_task.start_runner(iid, 1, 123, 456)
    agent_task.cancel(iid)
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (True, 456))
    agent_tick.reap(post=False)
    assert not agent_task.claim(other)
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (False, None))
    agent_tick.reap(post=False)
    assert agent_task.claim(other)


def test_v1_backup_migration_keeps_active_runner_and_is_idempotent(pool, tmp_path):
    import sqlite3
    iid = make_order(pool)["id"]
    store.transition(iid, "doing")
    store.update_item(iid, ext={agent_task.EXT_STATE: "running", agent_task.EXT_PID: 123,
                                agent_task.EXT_PSTART: 456, "unknown_canary": {"preserve": True}})
    with sqlite3.connect(pool) as conn:
        conn.execute("DROP TABLE agent_operations")
        conn.execute("PRAGMA user_version=1")
    backup = str(tmp_path / "backup.sqlite3")
    with sqlite3.connect(pool) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    store.init_db(backup)
    after = store.get_item(iid, db_path=backup)
    events = store.get_events(iid, db_path=backup)
    store.init_db(backup)
    assert store.get_item(iid, db_path=backup) == after
    assert store.get_events(iid, db_path=backup) == events
    assert after["state"] == "doing" and after["ext"]["unknown_canary"] == {"preserve": True}
    assert store.work_operation(iid, db_path=backup)["checkpoint"] == "legacy_running"


def test_real_runner_without_start_permission_never_calls_actor(pool, monkeypatch):
    import agent_run
    iid = make_order(pool)["id"]
    agent_task.claim(iid)
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (True, 456))
    monkeypatch.setattr(agent_run, "_llm", lambda *a, **k: pytest.fail("actor ran before spawn handshake"))
    assert agent_run.run_order(iid, post_reports=False, generation=1) == 2
    agent_task.begin_spawn(iid, 1)
    agent_task.cancel(iid)
    assert agent_run.run_order(iid, post_reports=False, generation=1) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PID identity")
def test_detached_child_owns_generation_when_parent_receipt_is_interrupted(pool, tmp_path, monkeypatch):
    import subprocess
    import time
    iid = make_order(pool)["id"]
    store.update_item(iid, ext={agent_task.EXT_WORKSPACE: str(tmp_path)})
    agent_task.claim(iid)
    runner = tmp_path / "synthetic_runner.py"
    scripts = str(Path(agent_task.__file__).parent)
    runner.write_text(
        "import sys,os,time,argparse\n"
        "sys.path.insert(0," + repr(scripts) + ")\n"
        "import agent_task\n"
        "p=argparse.ArgumentParser(); p.add_argument('--id'); p.add_argument('--generation',type=int); p.add_argument('--no-post',action='store_true'); a=p.parse_args()\n"
        "alive,start=agent_task.proc_identity(os.getpid())\n"
        "if alive and agent_task.start_runner(a.id,a.generation,os.getpid(),start):\n"
        " open('owned.txt','w').write(str(a.generation))\n"
        " time.sleep(0.25)\n"
        " agent_task.finish(a.id,False,'synthetic complete',generation=a.generation)\n"
        " agent_task.release(a.id,a.generation)\n", encoding="utf-8")
    monkeypatch.setattr(agent_tick, "RUNNER", str(runner))
    children = []
    original = subprocess.Popen
    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(agent_tick.subprocess, "Popen", spawn)
    monkeypatch.setattr(agent_task, "record_process", lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        with pytest.raises(KeyboardInterrupt):
            agent_tick.launch(agent_task.get(iid), generation=1, post_reports=False)
        children[0].wait(timeout=10)
        assert children[0].returncode == 0
        assert (tmp_path / "owned.txt").read_text() == "1"
        op = agent_task.operation(iid)
        # Windows venv redirectors may have a different PID from their Python worker.
        assert op["pid"] and op["pstart"] and op["released_at"]
        assert not agent_tick.launch(agent_task.get(iid), generation=1, post_reports=False)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_launcher_receipt_does_not_prevent_a_distinct_worker_pid_start(pool):
    iid = make_order(pool)["id"]
    agent_task.claim(iid)
    agent_task.begin_spawn(iid, 1)
    assert agent_task.record_process(iid, 123, 456, generation=1)
    assert agent_task.start_runner(iid, 1, 789, 101112)
    assert agent_task.record_process(iid, 123, 456, generation=1)
    op = agent_task.operation(iid)
    assert op["pid"] == 789 and op["launch_pid"] == 123
    assert agent_task.get(iid)["ext"][agent_task.EXT_PID] == 789


def test_preparing_work_cannot_be_claimed_and_cancel_prevents_publication(pool):
    item = store.add_item("FAKE_CANARY_PREPARING", source=agent_task.WORK_SOURCE,
                          ext={agent_task.EXT_STATE: "preparing"})
    assert not agent_task.claim(item["id"])
    agent_task.cancel(item["id"])
    assert store.publish_work(item["id"]) is None
    assert agent_task.get(item["id"])["state"] == "cancelled"


def test_missing_full_request_never_falls_back_to_truncated_title(pool):
    item = make_order(pool)
    with pytest.raises(OSError):
        agent_task.read_request(item)


def test_atomic_claim_across_independent_processes(pool, tmp_path):
    import subprocess
    items = [make_order(pool), make_order(pool)]
    scripts = str(Path(agent_task.__file__).parent)
    code = ("import sys; sys.path.insert(0," + repr(scripts) + "); "
            "import agent_task; print(int(agent_task.claim(sys.argv[1])))")
    children = [subprocess.Popen([sys.executable, "-c", code, item["id"]],
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                 creationflags=0x08000000 if sys.platform == "win32" else 0) for item in items]
    try:
        results = [child.communicate(timeout=15) for child in children]
        assert all(child.returncode == 0 for child in children), results
        assert sorted(int(out.strip()) for out, err in results) == [0, 1]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_finished_generation_never_accepts_another_terminal_outcome(pool):
    iid = make_order(pool)["id"]
    agent_task.claim(iid)
    assert not agent_task.finish(iid, True, generation=1), "unstarted work cannot be done"
    agent_task.begin_spawn(iid, 1)
    agent_task.start_runner(iid, 1, 123, 456)
    agent_task.checkpoint(iid, 1, "reviewing")
    assert agent_task.finish(iid, True, generation=1)
    assert not agent_task.finish(iid, False, generation=1)
    assert agent_task.operation(iid)["outcome"] == "done"
    events = store.get_events(iid)
    assert sum(event["event_type"] == "work_finish" for event in events) == 1


def test_runner_has_no_implicit_total_deadline(pool, monkeypatch, tmp_path):
    import agent_run
    from llmcall import process
    iid = make_order(pool)["id"]
    store.update_item(iid, ext={agent_task.EXT_WORKSPACE: str(tmp_path)})
    agent_task.claim(iid)
    agent_task.begin_spawn(iid, 1)
    d = Path(agent_task.run_dir(agent_task.get(iid), create=True))
    (d / "request.txt").write_text("SYNTHETIC_REQUEST", encoding="utf-8")
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (True, 456))
    def approach(*args, **kwargs):
        assert process.current_control().deadline is None
        assert process.current_control().cancellations
        agent_task.finish(iid, False, "synthetic stop", generation=1)
        return {"outcome": "failed"}
    monkeypatch.setattr(agent_run, "_run_approach", approach)
    assert agent_run.run_order(iid, post_reports=False, generation=1) == 1
    assert agent_task.operation(iid)["released_at"]
