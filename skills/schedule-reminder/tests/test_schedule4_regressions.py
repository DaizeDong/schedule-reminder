"""Synthetic behavior checks for the four Schedule4 review findings."""
import copy
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
import agent_run
import agent_task
import agent_tick
import digest
import dispatch
import ingest
import ingest_tick
import private_data
import store
from make_fixtures import (
    make_schedule4_hardlink, schedule4_cases, schedule4_order, write_schedule4_record,
)

F = schedule4_cases()


def writer_operation(tmp_path, monkeypatch, kind):
    if kind == "event":
        item = {"id": F["item_id"], "ext": {}}
        path = Path(agent_task.runs_root()) / item["id"] / "events.jsonl"
        return path, lambda: agent_task.append_event(item, "synthetic")
    if kind == "result":
        path = tmp_path / "result.txt"
        return path, lambda: agent_run._write(str(path), F["replacement"])
    if kind == "seen":
        path = tmp_path / "state/seen.json"
        monkeypatch.setattr(ingest, "_STATE_DIR", str(path.parent))
        monkeypatch.setattr(ingest, "_seen_file", lambda _: str(path))
        return path, lambda: ingest._save_seen(F["stream"], {F["message"]})
    if kind == "digest":
        path = tmp_path / "digest.json"
        monkeypatch.setattr(digest, "_path", lambda: str(path))
        return path, lambda: digest._save({"synthetic": True})
    if kind == "tick-log":
        path = tmp_path / "ingest.log"
        monkeypatch.setattr(ingest_tick, "_LOG", str(path))
        return path, lambda: ingest_tick._log(F["replacement"])
    if kind == "lock":
        path = tmp_path / "record.lock"
        def acquire():
            with private_data.file_lock(path):
                pass
        return path, acquire
    raise AssertionError("unknown generated writer case")


@pytest.mark.parametrize("kind", ["event", "result", "seen", "digest", "tick-log", "lock"])
def test_source4_writer_refuses_hardlink_before_mutation(tmp_path, monkeypatch, kind):
    source, operation = writer_operation(tmp_path, monkeypatch, kind)
    alias = tmp_path / "synthetic-public/record.txt"
    before = make_schedule4_hardlink(source, alias)
    error = None
    try:
        try:
            operation()
        except Exception as caught:
            error = caught
    finally:
        alias.unlink()
    assert source.lstat().st_nlink == 1
    assert source.read_bytes() == before
    assert isinstance(error, ValueError), repr(error)


@pytest.mark.parametrize("kind", ["event", "result", "seen", "digest", "tick-log", "lock"])
def test_source4_singleton_writer_control(tmp_path, monkeypatch, kind):
    source, operation = writer_operation(tmp_path, monkeypatch, kind)
    operation()
    assert source.is_file() and source.lstat().st_nlink == 1
    assert source.read_bytes()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_source4_database_alias_refused_before_sqlite_connect(tmp_path, monkeypatch, suffix):
    db = tmp_path / "schedule4.sqlite3"
    store.init_db(str(db))
    source = Path(str(db) + suffix)
    alias = tmp_path / "synthetic-public/database-record"
    before = make_schedule4_hardlink(source, alias)
    calls = []
    def forbidden_connect(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("synthetic sentinel: SQLite must not open an alias")
    monkeypatch.setattr(store.sqlite3, "connect", forbidden_connect)
    error = None
    try:
        try:
            store.add_item(F["title"], db_path=str(db))
        except Exception as caught:
            error = caught
    finally:
        alias.unlink()
    assert source.lstat().st_nlink == 1 and source.read_bytes() == before
    assert calls == []
    assert isinstance(error, store.SkillError)
    assert error.error_code == "ERR_DATA_POLICY"


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "schedule4.sqlite3")
    store.init_db(path)
    return path


@pytest.mark.parametrize("dependency_state", ["pending", "doing", "cancelled", "missing"])
def test_source4_done_creation_requires_satisfied_dependencies(db, dependency_state):
    if dependency_state == "missing":
        target = F["missing_dependency"]
    else:
        target = store.add_item(F["title"], state=dependency_state, db_path=db)["id"]
    before = store.list_items(db_path=db)["items"]
    with pytest.raises(store.SkillError) as caught:
        store.add_item(F["title"], state="done",
                       relations=[{"type": "depends-on", "target_id": target}], db_path=db)
    assert caught.value.error_code == "ERR_DEPENDENCY_UNMET"
    assert store.list_items(db_path=db)["items"] == before


@pytest.mark.parametrize("dependency", ["none", "done"])
def test_source4_blocked_creation_requires_actual_blocker(db, dependency):
    relations = None
    if dependency == "done":
        target = store.add_item(F["title"], state="done", db_path=db)["id"]
        relations = [{"type": "depends-on", "target_id": target}]
    before = store.list_items(db_path=db)["items"]
    with pytest.raises(store.SkillError) as caught:
        store.add_item(F["title"], state="blocked", relations=relations, db_path=db)
    assert caught.value.error_code == "ERR_BLOCK_REASON_REQUIRED"
    assert store.list_items(db_path=db)["items"] == before


@pytest.mark.parametrize("state", ["done", "blocked"])
def test_source4_valid_direct_state_controls(db, state):
    dependency = store.add_item(F["title"], state="done" if state == "done" else "pending", db_path=db)
    item = store.add_item(F["title"], state=state,
                          relations=[{"type": "depends-on", "target_id": dependency["id"]}], db_path=db)
    assert item["state"] == state
    if state == "done":
        assert item["progress"] == 100 and item["end_at"]


def test_source4_idempotent_return_keeps_existing_item(db):
    item = store.add_item(F["title"], idempotency_key=F["message"], db_path=db)
    replay = store.add_item(F["title"], state="blocked", idempotency_key=F["message"],
                            if_exists="return", db_path=db)
    assert replay == item
    assert len(store.list_items(db_path=db)["items"]) == 1


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    generated = schedule4_order(tmp_path)
    # Match shipped field constants while keeping the fixture generator provider-free.
    generated["ext"] = {
        agent_task.EXT_STATE: agent_task.STATE_QUEUED,
        agent_task.EXT_WORKSPACE: str(tmp_path), agent_task.EXT_STREAM: F["stream"],
    }
    db_path = str(tmp_path / "schedule4.sqlite3")
    monkeypatch.setenv("SCHEDULE_DB_PATH", db_path)
    store.init_db(db_path)
    item = store.add_item(generated["title"], state=generated["state"],
                          source=agent_task.WORK_SOURCE, ext=generated["ext"],
                          _id=generated["id"], db_path=db_path)
    live = {"value": False}
    launches, kills, events = [], [], []
    def rem(*args):
        def option(name, default=None):
            return args[args.index(name) + 1] if name in args else default
        item_id = option("--id")
        try:
            if args[0] == "get":
                current = store.get_item(item_id, db_path=db_path)
            elif args[0] == "update":
                current = store.update_item(item_id, ext=json.loads(option("--ext", "{}")),
                                            actor=agent_task.ACTOR, db_path=db_path)
            elif args[0] == "transition":
                current = store.transition(item_id, option("--to"), expect_state=option("--expect"),
                                           reason=option("--reason"), ext=json.loads(option("--ext", "{}")),
                                           actor=agent_task.ACTOR, db_path=db_path)
            else:
                raise AssertionError("unexpected lifecycle bridge operation")
        except store.SkillError as error:
            return {"_err": error.error_code}
        return {"item": current}
    def spawn(*args, **kwargs):
        launches.append(args)
        live["value"] = True
        return SimpleNamespace(pid=F["pid"])
    def kill(pid, pstart):
        if pid != F["pid"] or pstart != F["process_start"]:
            raise RuntimeError("synthetic process identity unavailable")
        kills.append((pid, pstart))
        was_live = live["value"]
        live["value"] = False
        return was_live
    monkeypatch.setattr(agent_task, "rem", rem)
    monkeypatch.setattr(agent_task, "orders", lambda active_only=True: store.list_items(
        source=agent_task.WORK_SOURCE, active_only=active_only, db_path=db_path)["items"])
    monkeypatch.setattr(agent_task, "append_event", lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(agent_task, "proc_identity", lambda _: (live["value"], F["process_start"]))
    monkeypatch.setattr(agent_task, "kill_tree", kill)
    monkeypatch.setattr(agent_tick, "_log", lambda *args: None)
    monkeypatch.setattr(agent_tick.subprocess, "Popen", spawn)
    class Lifecycle(SimpleNamespace):
        @property
        def item(self):
            return store.get_item(item["id"], db_path=db_path)
    return Lifecycle(live=live, launches=launches, kills=kills, events=events, spawn=spawn)


@pytest.mark.parametrize("claimed", [False, True])
def test_source4_stop_before_spawn_prevents_later_launch(lifecycle, claimed):
    stale = copy.deepcopy(lifecycle.item)
    if claimed:
        assert agent_task.claim(stale["id"])
    result = agent_tick.stop(stale["id"], post=False)
    assert result[0]["stopped"] is True
    assert lifecycle.item["state"] == "cancelled"
    assert agent_task.claim(stale["id"]) is False
    assert agent_tick.launch(stale) is False
    assert lifecycle.launches == [] and lifecycle.kills == []


def test_source4_stale_queued_stop_snapshot_uses_current_process(lifecycle, monkeypatch):
    stale = copy.deepcopy(lifecycle.item)
    first = True
    def orders(active_only=True):
        nonlocal first
        if first:
            first = False
            assert agent_task.claim(stale["id"])
            assert agent_tick.launch(agent_task.get(stale["id"]))
            return [stale]
        return [copy.deepcopy(lifecycle.item)]
    monkeypatch.setattr(agent_task, "orders", orders)
    result = agent_tick.stop(stale["id"], post=False)
    assert result[0]["stopped"] is True
    assert lifecycle.kills == [(F["pid"], F["process_start"])]
    assert lifecycle.live["value"] is False
    assert lifecycle.item["state"] == "cancelled"


def test_source4_unregistered_start_requires_cleanup_reconciliation(lifecycle):
    assert agent_task.claim(lifecycle.item["id"])
    assert agent_task.begin_spawn(lifecycle.item["id"], lifecycle.item["ext"][agent_task.EXT_GENERATION])
    result = agent_tick.stop(lifecycle.item["id"], post=False)
    assert result[0]["status"] == "reconcile" and not result[0]['stopped']
    assert lifecycle.item["state"] == "blocked"
    assert agent_task.exec_state(lifecycle.item) == 'reconcile'
    operation = agent_task.operation(lifecycle.item['id'])
    assert operation['cleanup_state'] == 'unknown' and operation['released_at'] is None


@pytest.mark.parametrize("termination_succeeds", [False, True])
def test_source4_registration_failure_tracks_or_terminates_spawn(lifecycle, monkeypatch, termination_succeeds):
    assert agent_task.claim(lifecycle.item["id"])
    monkeypatch.setattr(agent_task, "record_process", lambda *args, **kwargs: {"_err": "synthetic persistence failure"})
    if not termination_succeeds:
        def fail(*args):
            raise RuntimeError("synthetic termination unavailable")
        monkeypatch.setattr(agent_task, "kill_tree", fail)
        with pytest.raises(RuntimeError, match="termination is unproven"):
            agent_tick.launch(agent_task.get(lifecycle.item["id"]))
        assert lifecycle.item["state"] == "doing"
        assert agent_task.exec_state(lifecycle.item) == agent_task.STATE_STOPPING
        assert lifecycle.live["value"] is True
    else:
        assert agent_tick.launch(agent_task.get(lifecycle.item["id"])) is False
        assert lifecycle.item["state"] == "cancelled"
        assert lifecycle.live["value"] is False


def test_source4_stop_serializes_with_spawn_registration(lifecycle, monkeypatch):
    assert agent_task.claim(lifecycle.item["id"])
    entered, release, stop_started, stop_done = [threading.Event() for _ in range(4)]
    errors, stopped = [], []
    def spawn(*args, **kwargs):
        entered.set()
        assert release.wait(3), "synthetic spawn barrier timed out"
        return lifecycle.spawn(*args, **kwargs)
    monkeypatch.setattr(agent_tick.subprocess, "Popen", spawn)
    def launch():
        try:
            agent_tick.launch(agent_task.get(lifecycle.item["id"]))
        except BaseException as error:
            errors.append(error)
    def stop():
        stop_started.set()
        try:
            stopped.extend(agent_tick.stop(lifecycle.item["id"], post=False))
        except BaseException as error:
            errors.append(error)
        finally:
            stop_done.set()
    launch_thread = threading.Thread(target=launch)
    stop_thread = threading.Thread(target=stop)
    launch_thread.start()
    assert entered.wait(3)
    stop_thread.start()
    assert stop_started.wait(3)
    serialized = not stop_done.wait(.1)
    release.set()
    launch_thread.join(3)
    stop_thread.join(3)
    assert not launch_thread.is_alive() and not stop_thread.is_alive()
    assert errors == []
    assert serialized
    assert stopped[0]["stopped"] is True
    assert lifecycle.kills == [(F["pid"], F["process_start"])]
    assert lifecycle.item["state"] == "cancelled" and lifecycle.live["value"] is False


@pytest.mark.parametrize("route", ["relay", "channel"])
def test_source4_false_confirmation_retries_without_duplicate_actions(monkeypatch, route):
    plans, mutations, posts = [], [], []
    monkeypatch.setattr(dispatch, "get_state", lambda _: [])
    monkeypatch.setattr(dispatch, "get_work", lambda: [])
    monkeypatch.setattr(dispatch, "call_chain", lambda *args, **kwargs: plans.append(1) or json.dumps(F["plan"]))
    monkeypatch.setattr(dispatch, "_rem", lambda *args: mutations.append(args) or {"item": F["created"]})
    monkeypatch.setattr(dispatch, "_has_webhook", lambda _: route == "relay")
    def send(*args, **kwargs):
        posts.append((args, kwargs))
        return len(posts) > 1
    monkeypatch.setattr(dispatch.relay, "relay", send)
    monkeypatch.setattr(dispatch.relay, "send", send)
    kwargs = {"channel_id": "4102"} if route == "channel" else {}
    assert dispatch.dispatch(F["stream"], F["replacement"], msg_id=F["message"], **kwargs) is False
    assert dispatch.dispatch(F["stream"], F["replacement"], msg_id=F["message"], **kwargs) is True
    assert len(plans) == len(mutations) == 1
    assert len(posts) == 2


@pytest.mark.parametrize("post", [False, True])
def test_source4_confirmation_success_controls(monkeypatch, post):
    monkeypatch.setattr(dispatch, "get_state", lambda _: [])
    monkeypatch.setattr(dispatch, "get_work", lambda: [])
    monkeypatch.setattr(dispatch, "call_chain", lambda *args, **kwargs: json.dumps({"actions": []}))
    monkeypatch.setattr(dispatch.relay, "relay", lambda *args: True)
    assert dispatch.dispatch(F["stream"], F["replacement"], post=post) is True
