"""Generated inert regressions for Schedule7 atomicity, authorization and channel admission."""
import ast
import builtins
import contextlib
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import sqlite3
import stat
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).absolute().parents[3]
SOURCE_TEXT = globals().get("SOURCE_TEXT", {})


def source_text(relative):
    if relative in SOURCE_TEXT:
        return SOURCE_TEXT[relative]
    path = (ROOT / relative).absolute()
    for current in reversed((path, *path.parents)):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise AssertionError("Linked source path is not admitted")
        if current == path and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise AssertionError("Source must be one regular file")
    return path.read_text(encoding="utf-8")


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected external effect in Schedule7 control")


def module(relative, **dependencies):
    tree = ast.parse(source_text(relative), filename=relative)
    namespace = {"__name__": "schedule7_inert", "json": json, "copy": copy,
                 "re": re, "hashlib": hashlib, "datetime": datetime, "timezone": timezone,
                 "sqlite3": sqlite3, "Path": PurePosixPath,
                 "os": SimpleNamespace(environ={}, path=posixpath),
                 "time": SimpleNamespace(sleep=forbidden),
                 "__builtins__": dict(vars(builtins), open=forbidden, __import__=forbidden)}
    definitions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            definitions.append(node)
        elif isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    namespace[target.id] = value
    exec(compile(ast.Module(body=definitions, type_ignores=[]), relative, "exec"), namespace)
    namespace.update(dependencies)
    return SimpleNamespace(**namespace)


def fixtures():
    return module("tools/make_fixtures.py").schedule7_cases()


def storage():
    case = fixtures()
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row

    class Connection:
        execute = connection.execute
        executescript = connection.executescript

        def close(self):
            pass

    lock = SimpleNamespace(acquire=lambda: None, release=lambda: None)
    migration = module('skills/schedule-reminder/scripts/reminder_action_store.py')
    def owner_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'reminder_action_store' and tuple(fromlist) == ('migrate',) and level == 0:
            return SimpleNamespace(migrate=migration.migrate)
        return forbidden()
    store = module("skills/schedule-reminder/scripts/store.py",
                   _connect=lambda *args, **kwargs: Connection(), _WRITE_LOCK=lock,
                   default_db_path=lambda: "synthetic-memory",
                   os=SimpleNamespace(environ={}, path=SimpleNamespace(isfile=lambda path: True)),
                   now_utc=lambda: datetime(2031, 7, 12, 10, tzinfo=timezone.utc))
    # Functions capture their builtins at definition time. Admit only the parsed DDL helper.
    store.init_db.__builtins__['__import__'] = owner_import
    # Source functions retain their defining namespace; install deterministic identity there.
    sequence = iter(range(1, 100))
    store.add_item.__globals__["uuid7"] = lambda: "synthetic-item-" + str(next(sequence))
    store.init_db("synthetic-memory")
    return store, connection, case


class MemoryFile(io.StringIO):
    def __init__(self, world, path, mode):
        self.world, self.path, self.mode = world, world.key(path), mode
        if "r" in mode and self.path not in world.files:
            raise FileNotFoundError(self.path)
        if "w" in mode and world.fail_cursor and self.path.endswith(".last"):
            raise OSError(world.case["failure"])
        super().__init__(world.files.get(self.path, "") if "r" in mode else "")

    def fileno(self):
        return 0

    def close(self):
        if not self.closed and any(char in self.mode for char in "wa"):
            self.world.files[self.path] = self.getvalue()
        super().close()


class ChannelWorld:
    def __init__(self, registered=False):
        self.case = fixtures()
        self.files, self.pending, self.models = {}, {}, []
        self.fail_baseline = self.fail_cursor = self.fail_reactors = False
        self.messages = [copy.deepcopy(self.case["old_message"])]
        self.registered = registered
        self.stream = self.case["stream"] if registered else "#synthetic-new-channel"
        self.registry = {"reader": {"bot_token": self.case["token"]},
                         "guild_id": self.case["guild"],
                         "big_brother": {"user_id": self.case["owner"]},
                         "streams": ({self.stream: {"channel_id": self.case["channel"]}}
                                     if registered else {})}
        self.inbound = SimpleNamespace(stage=self.stage, pending=lambda root: list(self.pending.values()),
                                       _read=self.read_record, _write=self.write_record,
                                       processing=lambda record, root: contextlib.nullcontext(record),
                                       attempted=self.attempted)
        private = SimpleNamespace(prove_private=lambda *args: None,
                                  prepare_parent=lambda *args: None,
                                  open_for_write=self.open,
                                  file_lock=lambda *args: contextlib.nullcontext())
        fake_os = SimpleNamespace(path=SimpleNamespace(join=posixpath.join,
                                  exists=lambda path: self.key(path) in self.files,
                                  basename=posixpath.basename),
                                  makedirs=lambda *args, **kwargs: None, fsync=lambda *args: None)
        self.ingest = module("skills/schedule-reminder/scripts/ingest.py",
                             inbound=self.inbound, private_data=private, os=fake_os,
                             open=self.open, _STATE_DIR="/synthetic-state", _SAFE_KEY=re.compile(r"^[A-Za-z0-9_.-]+$"),
                             _fetch=self.fetch, _reactors=self.reactors,
                             _emoji_ref=lambda value: (value["name"], value["name"]),
                             channels=lambda *args, **kwargs: [(self.stream, self.case["channel"])],
                             load_registry=lambda: self.registry,
                             ack_seen=lambda *args: None, ack_done=lambda *args: None)
        self.tick = module("skills/schedule-reminder/scripts/ingest_tick.py",
                           ingest=self.ingest, inbound=self.inbound, _log=lambda *args: None,
                           dispatch=SimpleNamespace(dispatch=self.dispatch),
                           commands=SimpleNamespace(route=lambda messages, *args, **kwargs: ([], messages, [])))

    @staticmethod
    def key(path):
        return posixpath.normpath(str(path).replace("\\", "/"))

    def open(self, path, mode="r", **kwargs):
        return MemoryFile(self, path, mode)

    def read_record(self, path):
        value = self.files.get(self.key(path))
        return json.loads(value) if value is not None else None

    def write_record(self, path, value):
        if self.fail_baseline:
            raise OSError(self.case["failure"])
        self.files[self.key(path)] = json.dumps(value)

    def fetch(self, channel, token, after=None, limit=50):
        if after is not None:
            return copy.deepcopy([row for row in self.messages if int(row["id"]) > int(after)])
        return copy.deepcopy(self.messages[:limit])

    def reactors(self, *args, **kwargs):
        if self.fail_reactors:
            raise OSError(self.case["failure"])
        return [{"id": self.case["owner"], "bot": False}]

    def stage(self, stream, channel, identity, kind, payload, root):
        key = kind + ":" + str(identity)
        self.pending.setdefault(key, {"id": key, "stream": stream, "channel_id": channel,
                                     "message_id": identity, "kind": kind, "payload": payload,
                                     "status": "pending", "attempts": 0})
        return self.pending[key]

    def attempted(self, record, done, error, root, **kwargs):
        record.update(status="completed" if done else "pending", attempts=record["attempts"] + 1)

    def dispatch(self, *args, **kwargs):
        self.models.append((args, kwargs))
        return True

    def add_reaction(self):
        self.messages[-1]["reactions"].append(copy.deepcopy(self.case["new_reaction"]))


def dispatcher(items, work=()):
    case = fixtures()
    calls, stopped = [], []
    def rem(*args):
        calls.append(args)
        return {"item": {"id": args[2], "state": "done"}}
    def stop(identity, **kwargs):
        stopped.append(identity)
        expected = next(row for row in work if row['id'] == identity)['ext']['x_agent_exec_generation']
        assert kwargs.get('expected_generation') == expected
        selected = [row["id"] for row in work] if identity == "*" else [identity]
        return [{"id": item, "stopped": True} for item in selected]
    identity = lambda *values: hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    dispatch = module("skills/schedule-reminder/scripts/dispatch.py",
                      inbound=SimpleNamespace(identity=identity), _rem=rem,
                      agent_tick=SimpleNamespace(stop=stop),
                      agent_task=SimpleNamespace(enqueue=forbidden, EXT_GENERATION='x_agent_exec_generation'),
                      get_state=lambda cfg: copy.deepcopy(items), get_work=lambda: copy.deepcopy(work),
                      call_chain=forbidden, _post=lambda *args: True)
    return dispatch, calls, stopped, case


def bind_plan_identity(dispatch, record, stream):
    """Represent a saved current-format plan so authorization controls reach execution."""
    record['action_identity_version'] = 2
    record['action_keys'] = [dispatch._action_identity(stream, 'synthetic-inbound', index, action)
                             for index, action in enumerate(record['plan']['actions'])]


class Schedule7RegressionTests(unittest.TestCase):
    def test_rejected_block_preserves_item_and_events(self):
        for state, blocker_state in fixtures()["rejected_block_states"]:
            with self.subTest(state=state, blocker=blocker_state):
                store, connection, case = storage()
                try:
                    blocker = store.add_item(case["blocker_title"], state=blocker_state)
                    item = store.add_item(case["title"], state=state, ext=case["ext"])
                    before = (store.get_item(item["id"]), list(connection.execute("SELECT * FROM events")))
                    with self.assertRaises(store.SkillError):
                        store.block(item["id"], blocker_id=blocker["id"])
                    after = (store.get_item(item["id"]), list(connection.execute("SELECT * FROM events")))
                    self.assertEqual(after, before)
                finally:
                    connection.close()

    def test_block_event_failure_rolls_back_relation_and_state(self):
        store, connection, case = storage()
        try:
            blocker = store.add_item(case["blocker_title"])
            item = store.add_item(case["title"], ext=case["ext"])
            before = (store.get_item(item["id"]), list(connection.execute("SELECT * FROM events")))
            namespace = store.block.__globals__
            original = namespace["_append_event"]
            def fail_status(conn, item_id, actor, event_type, **kwargs):
                if event_type == "status_change":
                    raise OSError(case["failure"])
                return original(conn, item_id, actor, event_type, **kwargs)
            namespace["_append_event"] = fail_status
            with self.assertRaises(OSError):
                store.block(item["id"], blocker_id=blocker["id"])
            self.assertEqual((store.get_item(item["id"]), list(connection.execute("SELECT * FROM events"))), before)
        finally:
            connection.close()

    def test_block_success_commits_relation_state_and_preserves_ext(self):
        store, connection, case = storage()
        try:
            blocker = store.add_item(case["blocker_title"])
            item = store.add_item(case["title"], ext=case["ext"])
            result = store.block(item["id"], blocker_id=blocker["id"])
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(result["relations"], [{"type": "depends-on", "target_id": blocker["id"]}])
            self.assertEqual(result["ext"], case["ext"])
            self.assertEqual(store.get_item(item["id"]), result)
        finally:
            connection.close()

    def test_block_reason_without_relation_retains_existing_contract(self):
        store, connection, case = storage()
        try:
            item = store.add_item(case["title"])
            result = store.block(item["id"], reason=case["reason"])
            self.assertEqual((result["state"], result["block_reason"]), ("blocked", case["reason"]))
            self.assertIsNone(result["relations"])
        finally:
            connection.close()

    def test_persisted_retry_cannot_mutate_a_newly_visible_item(self):
        for operation in fixtures()["mutation_ops"]:
            with self.subTest(operation=operation):
                case = fixtures()
                items = [{"id": case["unshown_id"], "title": case["title"]}]
                dispatch, calls, _, _ = dispatcher(items)
                record = {"plan": {"actions": [{"op": operation, "id": case["unshown_id"],
                                                "until": case["until"]}]},
                          "outcomes": {}, "authorized_ids": [case["authorized_id"]]}
                bind_plan_identity(dispatch, record, case['stream'])
                self.assertFalse(dispatch._dispatch(case["stream"], case["title"], None, False,
                                                     case["channel"], case["message_id"],
                                                     record, lambda: None, "synthetic-inbound"))
                self.assertEqual(calls, [])
                self.assertEqual(next(iter(record["outcomes"].values()))["status"], "rejected")

    def test_authorized_current_item_can_mutate(self):
        for operation in fixtures()["mutation_ops"]:
            with self.subTest(operation=operation):
                case = fixtures()
                items = [{"id": case["authorized_id"], "title": case["title"]}]
                dispatch, calls, _, _ = dispatcher(items)
                result = dispatch.execute(case["stream"], {"kind": "reminder"},
                    {"actions": [{"op": operation, "id": case["authorized_id"], "until": case["until"]}]},
                    items, authorized_ids=[case["authorized_id"]])
                self.assertEqual(len(calls), 1)
                self.assertEqual(result["outcomes"][0]["status"], "succeeded")

    def test_authorized_completed_item_reconciles_read_only(self):
        case = fixtures()
        dispatch, calls, _, _ = dispatcher([])
        result = dispatch.execute(case["stream"], {"kind": "reminder"},
             {"actions": [{"op": "done", "id": case["authorized_id"]}]}, [],
             authorized_ids=[case["authorized_id"]])
        self.assertEqual(calls, [("get", "--id", case["authorized_id"])])
        self.assertEqual(result["done"], 1)

    def test_unshown_completed_item_is_not_even_read(self):
        case = fixtures()
        dispatch, calls, _, _ = dispatcher([])
        result = dispatch.execute(case["stream"], {"kind": "reminder"},
             {"actions": [{"op": "done", "id": case["unshown_id"]}]}, [],
             authorized_ids=[case["authorized_id"]])
        self.assertEqual(calls, [])
        self.assertEqual(result["done"], 0)

    def test_missing_saved_authorization_refuses_persisted_mutation(self):
        case = fixtures()
        items = [{"id": case["unshown_id"], "title": case["title"]}]
        dispatch, calls, _, _ = dispatcher(items)
        record = {"plan": {"actions": [{"op": "done", "id": case["unshown_id"]}]}, "outcomes": {}}
        bind_plan_identity(dispatch, record, case['stream'])
        self.assertFalse(dispatch._dispatch(case["stream"], case["title"], None, False,
                                             case["channel"], case["message_id"], record,
                                             lambda: None, "synthetic-inbound"))
        self.assertEqual(calls, [])

    def test_persisted_stop_cannot_expand_to_new_work(self):
        for selected in ("*", fixtures()["later_work_id"]):
            with self.subTest(selected=selected):
                case = fixtures()
                work = [{"id": case["work_id"], "title": case["title"],
                         'ext': {'x_agent_exec_generation': 1}},
                        {"id": case["later_work_id"], "title": case["title"],
                         'ext': {'x_agent_exec_generation': 1}}]
                dispatch, _, stops, _ = dispatcher([], work)
                record = {"plan": {"actions": [{"op": "stop", "id": selected}]}, "outcomes": {},
                          "authorized_ids": [], "authorized_work_ids": [case["work_id"]],
                          'authorized_work_generations': {case['work_id']: 1}}
                bind_plan_identity(dispatch, record, case['stream'])
                succeeded = dispatch._dispatch(case["stream"], case["title"], None, False,
                                               case["channel"], case["message_id"], record,
                                               lambda: None, "synthetic-inbound")
                self.assertEqual(succeeded, selected == "*")
                self.assertEqual(stops, [case["work_id"]] if selected == "*" else [])

    def test_first_channel_tick_baselines_text_and_reactions(self):
        for registered in (False, True):
            with self.subTest(registered=registered):
                world = ChannelWorld(registered)
                world.tick.run(post=False)
                self.assertEqual(world.models, [])
                self.assertEqual(world.pending, {})
                self.assertEqual(world.files[world.key(world.ingest._last_file(world.case["channel"]))],
                                 world.case["old_message"]["id"])

    def test_new_reaction_and_text_after_admission_are_processed_once(self):
        world = ChannelWorld()
        world.tick.run(post=False)
        world.models.clear()
        world.add_reaction()
        world.messages.insert(0, copy.deepcopy(world.case["new_message"]))
        world.tick.run(post=False)
        self.assertEqual(len(world.models), 2)
        kinds = {record["kind"] for record in world.pending.values()}
        self.assertEqual(kinds, {"text", "reaction"})
        self.assertTrue(all(record["status"] == "completed" for record in world.pending.values()))
        world.tick.run(post=False)
        self.assertEqual(len(world.models), 2)

    def test_baseline_failure_does_not_admit_or_dispatch_channel(self):
        for failure in ("fail_baseline", "fail_reactors"):
            with self.subTest(failure=failure):
                world = ChannelWorld()
                setattr(world, failure, True)
                world.tick.run(post=False)
                self.assertNotIn(world.key(world.ingest._last_file(world.case["channel"])), world.files)
                self.assertEqual(world.pending, {})
                self.assertEqual(world.models, [])
                setattr(world, failure, False)
                world.tick.run(post=False)
                self.assertEqual(world.models, [])

    def test_cursor_retry_preserves_original_baseline_and_later_reaction(self):
        world = ChannelWorld()
        world.fail_cursor = True
        with self.assertRaises(OSError):
            world.ingest.poll_stream(world.stream, world.case["channel"], world.case["token"], world.case["owner"])
        world.add_reaction()
        world.fail_cursor = False
        world.tick.run(post=False)
        self.assertEqual(len(world.models), 1)
        record = next(iter(world.pending.values()))
        self.assertEqual(record["payload"]["emoji"], world.case["new_reaction"]["emoji"]["name"])

    def test_reaction_only_discovery_arms_before_processing(self):
        world = ChannelWorld()
        self.assertEqual(world.ingest.poll_all_reactions(), {})
        self.assertEqual(world.pending, {})
        world.add_reaction()
        self.assertEqual(world.ingest.poll_all_reactions(), {world.stream: 1})
        self.assertEqual(len(world.pending), 1)

    def test_missing_saved_work_authorization_cannot_stop_any_work(self):
        case = fixtures()
        work = [{"id": case["work_id"], "title": case["title"]}]
        dispatch, _, stops, _ = dispatcher([], work)
        record = {"plan": {"actions": [{"op": "stop", "id": "*"}]},
                  "outcomes": {}, "authorized_ids": []}
        bind_plan_identity(dispatch, record, case['stream'])
        self.assertFalse(dispatch._dispatch(case["stream"], case["title"], None, False,
                         case["channel"], case["message_id"], record, lambda: None, "synthetic-inbound"))
        self.assertEqual(stops, [])

    def test_new_plan_persists_both_authorization_snapshots(self):
        case = fixtures()
        items = [{"id": case["authorized_id"], "title": case["title"]}]
        work = [{"id": case["work_id"], "title": case["title"], 'ext': {'x_agent_exec_generation': 1}}]
        dispatch, _, _, _ = dispatcher(items, work)
        namespace = dispatch._dispatch.__globals__
        namespace["build_prompt"] = lambda *args: case["title"]
        namespace["call_chain"] = lambda *args, **kwargs: json.dumps({"actions": []})
        record = {"outcomes": {}}
        self.assertTrue(dispatch._dispatch(case["stream"], case["title"], None, False,
                        case["channel"], case["message_id"], record, lambda: None, "synthetic-inbound"))
        self.assertEqual(record["authorized_ids"], [case["authorized_id"]])
        self.assertEqual(record["authorized_work_ids"], [case["work_id"]])
        self.assertEqual(record['authorized_work_generations'], {case['work_id']: 1})
        self.assertEqual(record['action_identity_version'], 2)
        self.assertEqual(record['action_keys'], [])

    def test_restart_and_missing_seen_projection_preserve_initial_baseline(self):
        first = ChannelWorld()
        first.tick.run(post=False)
        first.files.pop(first.key(first.ingest._seen_file(first.ingest._key(
                        first.stream, first.case["channel"]))), None)
        restarted = ChannelWorld()
        restarted.files = copy.deepcopy(first.files)
        restarted.tick.run(post=False)
        self.assertEqual(restarted.models, [])
        restarted.add_reaction()
        restarted.tick.run(post=False)
        self.assertEqual(len(restarted.models), 1)

    def test_existing_text_cursor_does_not_swallow_pending_reactions(self):
        world = ChannelWorld()
        world.files[world.key(world.ingest._last_file(world.case["channel"]))] = world.case["old_message"]["id"]
        world.tick.run(post=False)
        self.assertEqual(len(world.models), 1)
        self.assertEqual(next(iter(world.pending.values()))["kind"], "reaction")

    def test_cursor_retry_preserves_text_added_after_the_baseline(self):
        world = ChannelWorld()
        world.fail_cursor = True
        with self.assertRaises(OSError):
            world.ingest.poll_stream(world.stream, world.case["channel"], world.case["token"], world.case["owner"])
        world.messages.insert(0, copy.deepcopy(world.case["new_message"]))
        world.fail_cursor = False
        world.tick.run(post=False)
        world.tick.run(post=False)
        self.assertEqual(len(world.models), 1)
        self.assertEqual(next(iter(world.pending.values()))["message_id"], world.case["new_message"]["id"])

    def test_strict_reactor_snapshot_rejects_failure_and_truncation(self):
        case = fixtures()
        def failed_fetch(*args):
            raise OSError(case["failure"])
        ingest = module("skills/schedule-reminder/scripts/ingest.py", _get=failed_fetch)
        args = (case["channel"], case["message_id"], case["old_message"]["reactions"][0]["emoji"]["name"], case["token"])
        with self.assertRaises(OSError):
            ingest._reactors(*args, strict=True)
        self.assertEqual(ingest._reactors(*args), [])
        ingest._reactors.__globals__["_get"] = lambda *args: [{"id": case["owner"]}] * 2
        with self.assertRaises(RuntimeError):
            ingest._reactors(*args, limit=2, strict=True)
        self.assertEqual(ingest._reactors(*args, limit=3, strict=True), [{"id": case["owner"]}] * 2)



if __name__ == "__main__":
    unittest.main()
