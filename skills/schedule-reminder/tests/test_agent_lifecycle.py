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
from tools.make_fixtures import cleanup_call_result, cleanup_recovery_evidence
from test_agent_review_evidence import real_process


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


def started_order(pool):
    iid = make_order(pool)['id']
    agent_task.claim(iid)
    agent_task.begin_spawn(iid, 1)
    agent_task.start_runner(iid, 1, 123, 456)
    return iid


def test_stop_intent_revokes_execution_before_termination_receipt(pool):
    iid = started_order(pool)
    assert agent_task.child_started(iid, 1, {"phase": "actor"})
    assert not agent_task.request_stop(iid, "synthetic stop").get("_err")
    assert not agent_task.owns(iid, 1)
    assert not agent_task.checkpoint(iid, 1, "reviewing")
    assert not agent_task.finish(iid, True, generation=1)
    assert not agent_task.child_started(iid, 1, {"phase": "late reviewer"})
    assert agent_task.child_finished(iid, 1, {"phase": "actor", "cleanup_confirmed": True}, quiescent=True)
    assert agent_task.operation(iid)["released_at"] is None


def test_tick_running_count_includes_unmigrated_legacy_work(pool, monkeypatch):
    store.add_item("Synthetic legacy work", source=agent_task.WORK_SOURCE, state="doing",
                   ext={agent_task.EXT_STATE: "running"})
    monkeypatch.setattr(agent_tick, "reap", lambda *args, **kwargs: [])
    result = agent_tick.run(post=False, reap_only=True)
    assert result["running"] == 1 and result["launched"] is None


def test_stopped_spawn_without_identity_keeps_reservation_until_reviewed_cleanup(pool, monkeypatch):
    iid = make_order(pool)['id']
    assert agent_task.claim(iid)
    assert agent_task.begin_spawn(iid, 1)
    monkeypatch.setattr(agent_task, 'kill_tree', lambda *a: pytest.fail('no known process identity'))
    result = agent_tick.stop(iid, post=False)
    assert result[0]['stopped'] is False and result[0]['status'] == 'reconcile'
    op = agent_task.operation(iid)
    assert op['outcome'] == 'reconcile' and op['cleanup_state'] == 'unknown'
    assert op['released_at'] is None and not agent_task.start_runner(iid, 1, 123, 456)
    for _ in range(2):
        agent_tick.reap(post=False)
        assert agent_task.operation(iid)['released_at'] is None
    waiting = make_order(pool)['id']
    assert not agent_task.claim(waiting)
    assert agent_task.recover_cleanup(iid, 1, cleanup_recovery_evidence())
    assert agent_task.operation(iid)['released_at'] is not None
    assert agent_task.claim(waiting)


def test_unregistered_stop_reconciliation_rejects_stale_process_snapshot(pool):
    iid = make_order(pool)['id']
    assert agent_task.claim(iid)
    assert agent_task.begin_spawn(iid, 1)
    snapshot = agent_task.operation(iid)
    assert agent_task.record_process(iid, 123, 456, generation=1)
    assert not agent_task.request_stop(iid).get('_err')
    assert not store.advance_work(iid, 1, 'stop_reconcile', expected=snapshot, note='synthetic stop')
    current = agent_task.operation(iid)
    assert current['launch_pid'] == 123 and current['outcome'] is None


def test_stale_stop_generation_never_revokes_or_kills_reopened_work(pool, monkeypatch):
    iid = make_order(pool)['id']
    assert agent_task.claim(iid)
    assert not agent_task.cancel(iid).get('_err')
    assert agent_task.release(iid, 1)
    store.transition(iid, 'pending')
    store.update_item(iid, ext={agent_task.EXT_STATE: 'queued'})
    assert agent_task.claim(iid)
    assert agent_task.begin_spawn(iid, 2)
    assert agent_task.start_runner(iid, 2, 123, 456)
    monkeypatch.setattr(agent_task, 'kill_tree', lambda *a: pytest.fail('stale stop killed replacement'))
    for generation in (0, 1):
        result = agent_tick.stop(iid, post=False, expected_generation=generation)
        assert result[0]['status'] == 'stale_generation' and result[0]['stopped'] is False
        assert agent_task.owns(iid, 2)
    assert agent_task.request_stop(iid, expected_generation=1).get('_err')
    assert agent_task.owns(iid, 2)
    assert not agent_task.request_stop(iid, expected_generation=2).get('_err')
    assert not agent_task.owns(iid, 2)


def test_legacy_stop_requires_absence_of_generation(pool, monkeypatch):
    item = make_order(pool)
    monkeypatch.setattr(agent_task, 'kill_tree', lambda *a: pytest.fail('queued legacy work never started'))
    result = agent_tick.stop(item['id'], post=False, expected_generation=0)
    assert result[0]['stopped'] is True
    assert store.get_item(item['id'])['state'] == 'cancelled'


@pytest.mark.parametrize('race', ['after_intent', 'during_cleanup'])
def test_generation_replacement_during_stop_cannot_be_killed_or_cancelled(pool, monkeypatch, race):
    iid = started_order(pool)
    def replace():
        assert not agent_task.cancel(iid).get('_err')
        assert agent_task.release(iid, 1)
        store.transition(iid, 'pending')
        store.update_item(iid, ext={agent_task.EXT_STATE: 'queued'})
        assert agent_task.claim(iid)
        assert agent_task.begin_spawn(iid, 2)
        assert agent_task.start_runner(iid, 2, 789, 900)
    stopped = []
    if race == 'after_intent':
        original = agent_task.request_stop
        def request(*args, **kwargs):
            result = original(*args, **kwargs)
            replace()
            return result
        monkeypatch.setattr(agent_task, 'request_stop', request)
    def kill(pid, pstart):
        stopped.append((pid, pstart))
        assert (pid, pstart) == (123, 456)
        if race == 'during_cleanup':
            replace()
        return True
    monkeypatch.setattr(agent_task, 'kill_tree', kill)
    result = agent_tick.stop(iid, post=False, expected_generation=1)
    assert not result[0]['stopped']
    assert stopped == ([] if race == 'after_intent' else [(123, 456)])
    assert agent_task.owns(iid, 2) and store.get_item(iid)['state'] == 'doing'


def test_reopened_queue_is_not_the_terminal_generation_authorized_for_stop(pool, monkeypatch):
    iid = make_order(pool)['id']
    assert agent_task.claim(iid)
    assert not agent_task.cancel(iid).get('_err')
    assert agent_task.release(iid, 1)
    store.transition(iid, 'pending')
    store.update_item(iid, ext={agent_task.EXT_STATE: 'queued'})
    monkeypatch.setattr(agent_task, 'kill_tree', lambda *a: pytest.fail('terminal generation cannot stop reopened queue'))
    result = agent_tick.stop(iid, post=False, expected_generation=1)
    assert result[0]['status'] == 'stale_generation'
    assert not store.cancel_work(iid, 1, note='stale synthetic cancellation')
    assert store.get_item(iid)['state'] == 'pending'


def test_terminal_followup_receipt_is_readonly_and_binds_note(pool, monkeypatch):
    item = store.add_item("Synthetic follow-up", source="synthetic-source")
    store.append_dispatch_note(item["id"], "Synthetic note", "synthetic-followup")
    store.done(item["id"])
    events = store.get_events(item["id"])
    monkeypatch.setattr(store, "init_db", lambda *a, **k: pytest.fail("read migrated database"))
    assert store.dispatch_note_receipt(item["id"], "Synthetic note", "synthetic-followup")["state"] == "done"
    assert store.dispatch_note_receipt(item["id"], "Synthetic note", "missing-followup") is None
    with pytest.raises(store.SkillError) as conflict:
        store.dispatch_note_receipt(item["id"], "Changed note", "synthetic-followup")
    assert conflict.value.error_code == "ERR_CONFLICT"
    assert store.get_events(item["id"]) == events


def test_creation_receipt_recovers_cross_source_reuse_after_completion(pool, monkeypatch):
    original = store.ensure_item("Synthetic shared obligation", source="synthetic-source-a",
                                 idempotency_key="synthetic-key-a")["item"]
    reused = store.ensure_item("Synthetic shared obligation", source="synthetic-source-b",
                               idempotency_key="synthetic-key-b")["item"]
    assert reused["id"] == original["id"]
    store.done(original["id"])
    events = store.get_events(original["id"])
    monkeypatch.setattr(store, "init_db", lambda *a, **k: pytest.fail("read migrated database"))
    recovered = store.creation_receipt_item("synthetic-source-b", "synthetic-key-b")
    assert recovered["id"] == original["id"] and recovered["state"] == "done"
    assert store.creation_receipt_item("synthetic-source-b", "missing-key") is None
    assert store.get_events(original["id"]) == events


def test_creation_receipt_legacy_fallback_rejects_different_source(pool):
    item = store.add_item("Synthetic legacy receipt", source="synthetic-source", idempotency_key="synthetic-key")
    assert store.creation_receipt_item("synthetic-source", "synthetic-key")["id"] == item["id"]
    with pytest.raises(store.SkillError) as conflict:
        store.creation_receipt_item("different-source", "synthetic-key")
    assert conflict.value.error_code == "ERR_CONFLICT"


def test_legacy_stop_pending_keeps_writer_reserved(pool):
    store.add_item("Synthetic stopping legacy work", source=agent_task.WORK_SOURCE,
                   ext={agent_task.EXT_STATE: "stop_pending"})
    assert not store.claim_work(make_order(pool)["id"])


@pytest.mark.parametrize("raw", ["invalid-json", "{}", '{"item_id":"missing","request_digest":"' + "0" * 64 + '"}'])
def test_creation_receipt_rejects_corrupt_or_missing_references(pool, raw):
    import creation_guard
    key = "creation:" + creation_guard._digest(["synthetic-source", "synthetic-key"])
    conn = store.admitted_connection(pool)
    try:
        with store._Tx(conn):
            conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (key, raw))
    finally:
        conn.close()
    with pytest.raises(store.SkillError) as conflict:
        store.creation_receipt_item("synthetic-source", "synthetic-key")
    assert conflict.value.error_code == "ERR_CONFLICT"


def test_receipt_readers_never_create_missing_storage(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    for read in (lambda: store.dispatch_note_receipt("synthetic-item", "Synthetic note", "synthetic-key", db_path=str(missing)),
                 lambda: store.creation_receipt_item("synthetic-source", "synthetic-key", db_path=str(missing))):
        with pytest.raises(store.SkillError) as error:
            read()
        assert error.value.error_code == "ERR_UNINITIALIZED"
        assert not missing.exists()


def test_cleaned_timeout_releases_queue_without_replaying_failed_work(pool, monkeypatch):
    import agent_run
    iid = started_order(pool)
    result = cleanup_call_result(True)
    token = agent_run._OPERATION.set((iid, 1))
    try:
        assert agent_run._observed_call('actor', lambda: result) is result
    finally:
        agent_run._OPERATION.reset(token)
    assert agent_task.finish(iid, False, 'outcome requires review', exec_state_value='reconcile', generation=1)
    monkeypatch.setattr(agent_task, 'proc_identity', lambda pid: (False, None))
    agent_tick.reap(post=False)
    assert agent_task.get(iid)['state'] == 'blocked'
    assert agent_task.operation(iid)['released_at']
    assert not agent_task.claim(iid)
    assert agent_task.claim(make_order(pool)['id'])


@pytest.mark.parametrize('confirmed', [None, False])
def test_unconfirmed_timeout_still_reserves_writer(pool, monkeypatch, confirmed):
    import agent_run
    iid = started_order(pool)
    token = agent_run._OPERATION.set((iid, 1))
    try:
        with pytest.raises(agent_run.CleanupUncertain):
            agent_run._observed_call('actor', lambda: cleanup_call_result(confirmed))
    finally:
        agent_run._OPERATION.reset(token)
    agent_task.cancel(iid)
    monkeypatch.setattr(agent_task, 'proc_identity', lambda pid: (False, None))
    agent_tick.reap(post=False)
    assert not agent_task.claim(make_order(pool)['id'])


def interrupted_order(pool):
    iid = started_order(pool)
    agent_task.child_started(iid, 1, {'phase': 'actor'})
    agent_task.child_finished(iid, 1, {'outcome': 'timeout'}, quiescent=False)
    agent_task.cancel(iid)
    return iid


def test_reviewed_cleanup_recovery_preserves_cancelled_state_and_history(pool, monkeypatch):
    iid = interrupted_order(pool)
    monkeypatch.setattr(agent_task, 'proc_identity', lambda pid: (False, None))
    assert agent_task.recover_cleanup(iid, 1, cleanup_recovery_evidence())
    op = agent_task.operation(iid)
    assert op['released_at'] and op['cleanup_state'] == 'quiescent'
    assert agent_task.get(iid)['state'] == 'cancelled'
    assert not agent_task.claim(iid)
    assert agent_task.claim(make_order(pool)['id'])


@pytest.mark.parametrize('identity', [(True, 456), (True, None)])
def test_recovery_refuses_live_or_unqueryable_runner(pool, monkeypatch, identity):
    iid = interrupted_order(pool)
    monkeypatch.setattr(agent_task, 'proc_identity', lambda pid: identity)
    with pytest.raises(ValueError):
        agent_task.recover_cleanup(iid, 1, cleanup_recovery_evidence())
    assert not agent_task.operation(iid)['released_at']


def test_recovery_requires_exact_generation_and_review_evidence(pool, monkeypatch):
    iid = interrupted_order(pool)
    monkeypatch.setattr(agent_task, 'proc_identity', lambda pid: (False, None))
    for generation, evidence in [(2, cleanup_recovery_evidence()), (1, {})]:
        with pytest.raises(ValueError):
            agent_task.recover_cleanup(iid, generation, evidence)
    assert not agent_task.operation(iid)['released_at']


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
        if str(runner) in args[0]:
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


def test_runner_has_no_implicit_total_deadline(pool, monkeypatch, tmp_path, real_process):
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
