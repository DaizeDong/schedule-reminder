"""Generated synthetic controls for command and notification failure recovery."""
import contextlib
import copy
import io
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import agent_task
import agent_tick
import commands
import dispatch
import inbound
import ingest
import ingest_tick
import reminder
import store
from make_fixtures import schedule6_cases

F = schedule6_cases()


def database(tmp_path):
    path = str(tmp_path / "schedule6.sqlite3")
    store.init_db(path)
    return path


def command_inbox(tmp_path, monkeypatch, outcome=False):
    root = tmp_path / "state"
    monkeypatch.setattr(ingest, "state_dir", lambda: str(root))
    monkeypatch.setattr(ingest, "pending_work", lambda: inbound.pending(root))
    monkeypatch.setattr(ingest, "poll", lambda **kwargs: {})
    monkeypatch.setattr(ingest, "load_registry", lambda: copy.deepcopy(F["registry"]))
    monkeypatch.setattr(ingest, "bot_token", lambda _: "synthetic-token")
    monkeypatch.setattr(ingest_tick, "_log", lambda *args: None)
    calls, acks, models = [], [], []
    monkeypatch.setattr(commands, "run", lambda *args, **kwargs: calls.append(args) or (outcome, F["error"]))
    monkeypatch.setattr(ingest, "ack_seen", lambda *args: None)
    monkeypatch.setattr(ingest, "ack_done", lambda *args: acks.append(args))
    monkeypatch.setattr(dispatch, "dispatch", lambda *args, **kwargs: models.append(args) or True)
    record = inbound.stage(F["stream"], F["channel"], F["message"]["id"], "text", F["message"], root)
    return SimpleNamespace(root=root, record=record, calls=calls, acks=acks, models=models)


def read_record(case):
    return inbound._read(case.root / "inbound" / (case.record["id"] + ".json"))


@pytest.mark.parametrize("outcome", [False, True])
def test_command_completion_requires_handler_success(tmp_path, monkeypatch, outcome):
    case = command_inbox(tmp_path, monkeypatch, outcome)
    result = ingest_tick.run(post=False)
    assert len(case.calls) == 1 and case.models == []
    assert read_record(case)["status"] == ("completed" if outcome else "command_failed")
    assert bool(case.acks) is outcome
    assert result["handled"][F["stream"]] == ("ok" if outcome else "failed")
    ingest_tick.run(post=False)
    assert len(case.calls) == 1


def test_failed_command_requires_explicit_retry(tmp_path, monkeypatch):
    case = command_inbox(tmp_path, monkeypatch)
    ingest_tick.run(post=False)
    inbound.retry_command(case.record["id"], case.root)
    monkeypatch.setattr(commands, "run", lambda *args, **kwargs: case.calls.append(args) or (True, ""))
    ingest_tick.run(post=False)
    assert len(case.calls) == 2 and len(case.acks) == 1 and not case.models
    current = read_record(case)
    assert current["status"] == "completed" and current["manual_retries"] == 1
    assert current["attempts"] == 2
    with pytest.raises(ValueError):
        inbound.retry_command(case.record["id"], case.root)


def test_unconfirmed_command_is_not_replayed(tmp_path, monkeypatch):
    case = command_inbox(tmp_path, monkeypatch)
    def uncertain(*args, **kwargs):
        case.calls.append(args)
        raise RuntimeError(F["error"])
    monkeypatch.setattr(commands, "run", uncertain)
    ingest_tick.run(post=False)
    ingest_tick.run(post=False)
    assert len(case.calls) == 1 and not case.acks and not case.models
    assert read_record(case)["status"] == "command_failed"


def test_command_intent_failure_prevents_handler(tmp_path, monkeypatch):
    case = command_inbox(tmp_path, monkeypatch)
    original = inbound._write
    def fail_intent(path, value):
        if value["status"] == "command_failed":
            raise OSError(F["error"])
        return original(path, value)
    monkeypatch.setattr(inbound, "_write", fail_intent)
    ingest_tick.run(post=False)
    assert not case.calls and not case.acks and not case.models
    assert read_record(case)["status"] == "pending"


def started_order(tmp_path, monkeypatch):
    """A running generation that has reached verification, so either terminal outcome is allowed."""
    db = database(tmp_path)
    monkeypatch.setenv('SCHEDULE_DB_PATH', db)
    ext = {**F["unknown_ext"], agent_task.EXT_STATE: agent_task.STATE_QUEUED}
    item = store.add_item(F["title"], source=agent_task.WORK_SOURCE, ext=ext, db_path=db)
    assert agent_task.claim(item["id"]) and agent_task.begin_spawn(item["id"], 1)
    assert agent_task.start_runner(item["id"], 1, 6102, 61)
    assert agent_task.checkpoint(item["id"], 1, "verifying")
    return SimpleNamespace(db=db, item=store.get_item(item["id"], db_path=db))


@pytest.mark.parametrize("outcome", [False, True])
def test_finish_failure_rolls_back_state_and_metadata(tmp_path, monkeypatch, outcome):
    case = started_order(tmp_path, monkeypatch)
    original = store._append_event
    def fail_terminal(conn, item_id, actor, kind, *args, **kwargs):
        if kind == "status_change":
            raise OSError(F["error"])
        return original(conn, item_id, actor, kind, *args, **kwargs)
    monkeypatch.setattr(store, "_append_event", fail_terminal)
    with pytest.raises(OSError):
        agent_task.finish(case.item["id"], outcome, F["note"], generation=1)
    current = store.get_item(case.item["id"], db_path=case.db)
    assert current["state"] == case.item["state"] and current["ext"] == case.item["ext"]
    assert agent_task.running([current]) == [current]
    assert agent_task.operation(case.item["id"])["outcome"] is None
    monkeypatch.setattr(store, "_append_event", original)
    assert agent_task.finish(case.item["id"], outcome, F["note"], generation=1)


@pytest.mark.parametrize("outcome", [False, True])
def test_finish_commits_state_with_execution_metadata(tmp_path, monkeypatch, outcome):
    case = started_order(tmp_path, monkeypatch)
    assert agent_task.finish(case.item["id"], outcome, F["note"], generation=1)
    current = store.get_item(case.item["id"], db_path=case.db)
    assert current["state"] == ("done" if outcome else "blocked")
    assert agent_task.exec_state(current) == (agent_task.STATE_DONE if outcome else agent_task.STATE_FAILED)
    assert all(current["ext"][key] == value for key, value in F["unknown_ext"].items())


def test_reaper_does_not_report_failed_persistence_as_reaped(tmp_path, monkeypatch):
    case = started_order(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (False, None))
    monkeypatch.setattr(agent_task, "reconcile", lambda *args, **kwargs: False)
    monkeypatch.setattr(agent_tick, "_post", lambda *args, **kwargs: pytest.fail("reported a failed reconcile"))
    monkeypatch.setattr(agent_tick, "_log", lambda *args: None)
    assert agent_tick.reap(post=True) == []
    assert agent_task.operation(case.item["id"])["outcome"] is None


def exhausted(db):
    item = store.add_item(F["title"], due_at=F["now"], db_path=db)
    calls = []
    when = F["now"]
    for _ in range(store._NOTIFY_MAX_RETRIES):
        store.tick(db_path=db, now=when, notify_fn=lambda value: calls.append(value["id"]) or False)
        current = store.get_item(item["id"], db_path=db)
        when = current["next_retry_at"] or when
    return item, calls, when


def test_delivery_retry_limit_stops_same_and_later_ticks(tmp_path):
    db = database(tmp_path)
    item, calls, when = exhausted(db)
    assert len(calls) == store._NOTIFY_MAX_RETRIES
    for stamp in [when, F["later"]]:
        store.tick(db_path=db, now=stamp, notify_fn=lambda value: calls.append(value["id"]) or False)
    assert len(calls) == store._NOTIFY_MAX_RETRIES
    current = store.get_item(item["id"], db_path=db)
    assert current["state"] == "blocked" and current["next_retry_at"] is None


def test_ordinary_blocked_task_can_still_receive_reminder(tmp_path):
    db = database(tmp_path)
    item = store.add_item(F["title"], due_at=F["now"], db_path=db)
    store.block(item["id"], reason=F["note"], db_path=db)
    calls = []
    result = store.tick(db_path=db, now=F["now"], notify_fn=lambda value: calls.append(value["id"]) or True)
    assert calls == [item["id"]] and result["dispatched"] == calls
    assert store.get_item(item["id"], db_path=db)["state"] == "blocked"


def test_snooze_explicitly_rearms_exhausted_delivery(tmp_path):
    db = database(tmp_path)
    item, calls, _ = exhausted(db)
    store.snooze(item["id"], F["later"], db_path=db)
    assert store.get_item(item["id"], db_path=db)["retry_count"] == 0
    result = store.tick(db_path=db, now=F["later"], notify_fn=lambda value: calls.append(value["id"]) or True)
    assert result["dispatched"] == [item["id"]]
    assert len(calls) == store._NOTIFY_MAX_RETRIES + 1


def metadata(db):
    conn = store._connect(db, readonly=True)
    try:
        return [tuple(row) for row in conn.execute("SELECT key,value FROM meta ORDER BY key")]
    finally:
        conn.close()


def test_preview_preserves_metadata_items_and_events(tmp_path):
    db = database(tmp_path)
    item = store.add_item(F["title"], due_at=F["now"], db_path=db)
    before = (metadata(db), store.get_item(item["id"], db_path=db), store.get_events(item["id"], db_path=db))
    calls = []
    result = store.tick(db_path=db, now=F["now"], dry_run=True, notify_fn=lambda value: calls.append(value) or True)
    after = (metadata(db), store.get_item(item["id"], db_path=db), store.get_events(item["id"], db_path=db))
    assert before == after and calls == []
    assert result["dispatched"] == [item["id"]]


def test_real_tick_deduplicates_delivery(tmp_path):
    db = database(tmp_path)
    item = store.add_item(F["title"], due_at=F["now"], db_path=db)
    calls = []
    for _ in range(2):
        store.tick(db_path=db, now=F["now"], notify_fn=lambda value: calls.append(value["id"]) or True)
    assert calls == [item["id"]]


def test_idle_tick_opens_no_write_connection(tmp_path, monkeypatch):
    """A tick with nothing due (and nothing lapsed) only reads; a claim opens the writer."""
    db = database(tmp_path)
    store.add_item(F["title"], due_at="2999-01-01T00:00:00Z", db_path=db)
    opened = []
    real = store._connect
    monkeypatch.setattr(store, "_connect",
                        lambda *a, **kw: opened.append(kw.get("readonly", False)) or real(*a, **kw))
    report = store.tick(db_path=db, now=F["now"], notify_fn=lambda value: True)
    assert report["dispatched"] == [] and opened == [True]
    store.add_item(F["title"], due_at=F["now"], db_path=db)
    opened.clear()
    assert len(store.tick(db_path=db, now=F["now"], notify_fn=lambda value: True)["dispatched"]) == 1
    assert opened == [True, False]


@pytest.mark.parametrize("readonly", [False, True])
def test_admitted_connection_walks_its_paths_once(tmp_path, monkeypatch, readonly):
    import private_data
    db = database(tmp_path)
    walks = []
    real = private_data.assert_sqlite_paths
    monkeypatch.setattr(private_data, "assert_sqlite_paths", lambda path: walks.append(path) or real(path))
    store.admitted_connection(db, readonly=readonly).close()
    assert len(walks) == 1


@pytest.mark.parametrize("outcome", [False, True])
def test_relay_bool_fallback_is_honoured_by_the_tick(tmp_path, monkeypatch, outcome):
    """relay.deliver returns a bare bool when the stream has no webhook/channel (Big Brother DM)."""
    import relay
    monkeypatch.delenv("SCHEDULE_RELAY_CMD", raising=False)
    monkeypatch.setattr(relay, "load_registry", lambda: {})
    calls = []
    monkeypatch.setattr(relay, "relay", lambda stream, text, username=None: calls.append(text) or outcome)
    db = database(tmp_path)
    item = store.add_item(F["title"], due_at=F["now"], db_path=db)
    report = store.tick(db_path=db, now=F["now"])
    assert len(calls) == 1 and F["title"] in calls[0]
    assert (report["dispatched"] == [item["id"]]) is outcome
    assert (report["retried"] == [item["id"]]) is (not outcome)


def test_slow_poll_does_not_starve_staged_records(tmp_path, monkeypatch):
    """A poll that spends the whole intake window must not stop already staged work."""
    case = command_inbox(tmp_path, monkeypatch, True)
    clock = [1000.0]
    monkeypatch.setattr(ingest_tick, "time",
                        SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda seconds: None))
    def slow_poll(**kwargs):
        clock[0] += ingest_tick.INTAKE_S + 30
        return {}
    monkeypatch.setattr(ingest, "poll", slow_poll)
    result = ingest_tick.run(post=False)
    assert len(case.calls) == 1
    assert result["handled"][F["stream"]] == "ok"
    assert read_record(case)["status"] == "completed"


def test_poll_that_eats_the_tick_leaves_staged_records_pending(tmp_path, monkeypatch):
    """No record starts with too little of the whole tick left; it waits instead of burning an attempt."""
    case = command_inbox(tmp_path, monkeypatch, True)
    clock = [1000.0]
    monkeypatch.setattr(ingest_tick, "time",
                        SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda seconds: None))
    def slow_poll(**kwargs):
        clock[0] += ingest_tick.TASK_LIMIT_S - ingest_tick.RESERVE_S - ingest_tick.MIN_DISPATCH_S + 5
        return {}
    monkeypatch.setattr(ingest, "poll", slow_poll)
    ingest_tick.run(post=False)
    assert case.calls == []
    record = read_record(case)
    assert record["status"] == "pending"
    assert not record["attempts"]
