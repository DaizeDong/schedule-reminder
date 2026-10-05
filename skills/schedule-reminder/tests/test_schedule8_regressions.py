"""Inert Schedule8 regressions; all records come from tools/make_fixtures.py."""
import argparse
import ast
import builtins
import contextlib
import copy
from datetime import datetime, timezone
from functools import wraps
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import sqlite3
import stat
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch
import offline_support
import private_data
from urllib.parse import urlsplit

import test_schedule7_regressions as support


def case():
    return support.module("tools/make_fixtures.py").schedule8_cases()


class DefinitionsOnly(ast.NodeTransformer):
    def visit_Import(self, node):
        return ast.copy_location(ast.Pass(), node)

    def visit_ImportFrom(self, node):
        return ast.copy_location(ast.Pass(), node)

    def visit_FunctionDef(self, node):
        node.decorator_list = []
        return self.generic_visit(node)


def inert_module(relative, **dependencies):
    tree = ast.parse(support.source_text(relative), filename=relative)
    namespace = {
        "__name__": "schedule8_inert", "json": json, "re": re, "stat": stat,
        "datetime": datetime, "timezone": timezone, "Path": PurePosixPath,
        "contextmanager": contextlib.contextmanager, "wraps": wraps,
        "os": SimpleNamespace(environ={}, path=posixpath),
        "sys": SimpleNamespace(stderr=io.StringIO()), "sqlite3": sqlite3,
        "__builtins__": dict(vars(builtins), open=support.forbidden,
                             __import__=support.forbidden),
    }
    definitions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            definitions.append(DefinitionsOnly().visit(node))
        elif isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (TypeError, ValueError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    namespace[target.id] = value
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for member, item in zip(target.elts, value):
                        if not isinstance(member, ast.Name):
                            raise AssertionError("Unsupported literal assignment target")
                        namespace[member.id] = item
    namespace.update(dependencies)
    compiled = compile(ast.fix_missing_locations(ast.Module(body=definitions, type_ignores=[])),
                       relative, "exec", flags=16777216)
    exec(compiled, namespace)
    namespace.update(dependencies)
    return SimpleNamespace(**namespace)


def storage():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row

    class Connection:
        execute = connection.execute
        executescript = connection.executescript

        def close(self):
            pass

    sequence = iter(range(100))
    store = inert_module("skills/schedule-reminder/scripts/store.py",
        _connect=lambda *args, **kwargs: Connection(),
        migrate=inert_module('skills/schedule-reminder/scripts/reminder_action_store.py').migrate,
        _WRITE_LOCK=SimpleNamespace(acquire=lambda: None, release=lambda: None),
        uuid7=lambda: "synthetic-item-" + str(next(sequence)),
        default_db_path=lambda: "synthetic-memory",
        now_utc=lambda: datetime(2031, 8, 12, tzinfo=timezone.utc),
        os=SimpleNamespace(environ={}, path=SimpleNamespace(isfile=lambda path: True)))
    store.init_db("synthetic-memory")
    return store, connection


def channel_world():
    world = support.ChannelWorld()
    original = world.fetch
    def fetch(channel, token, after=None, limit=50):
        return original(channel, token, after=after or None, limit=limit)
    world.ingest.poll_stream.__globals__["_fetch"] = fetch
    return world


class Schedule8Regressions(unittest.TestCase):
    def test_first_cursor_truncation_never_replays_prior_instructions(self):
        for truncated in ("", support.fixtures()["old_message"]["id"][:2]):
            with self.subTest(truncated=truncated):
                world = channel_world()
                world.fail_cursor = True
                with self.assertRaises(OSError):
                    world.ingest.poll_stream(world.stream, world.case["channel"],
                                             world.case["token"], world.case["owner"])
                cursor = world.key(world.ingest._last_file(world.case["channel"]))
                world.files[cursor] = truncated
                world.fail_cursor = False
                self.assertEqual(world.ingest.poll_stream(world.stream, world.case["channel"],
                                      world.case["token"], world.case["owner"]), [])
                self.assertEqual(world.pending, {})
                self.assertEqual(world.files[cursor], world.case["old_message"]["id"])

    def test_cursor_recovery_preserves_later_text_and_reaction(self):
        world = channel_world()
        world.fail_cursor = True
        with self.assertRaises(OSError):
            world.ingest.poll_stream(world.stream, world.case["channel"],
                                     world.case["token"], world.case["owner"])
        world.files[world.key(world.ingest._last_file(world.case["channel"]))] = ""
        world.fail_cursor = False
        world.add_reaction()
        world.messages.insert(0, copy.deepcopy(world.case["new_message"]))
        world.tick.run(post=False)
        world.tick.run(post=False)
        self.assertEqual({row["kind"] for row in world.pending.values()}, {"text", "reaction"})
        self.assertEqual(len(world.pending), 2)
        self.assertEqual(len(world.models), 2)

    def test_writer_stream_construction_failure_keeps_existing_bytes(self):
        original = case()["existing_record"]
        state = {"value": original, "closed": 0}
        inode = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1)
        path = SimpleNamespace(lstat=lambda: inode)
        def broken_stream(*args, **kwargs):
            raise OSError(case()["failure"])
        fake_os = SimpleNamespace(O_WRONLY=1, O_RDWR=2, O_CREAT=64, O_APPEND=1024,
            O_EXCL=128, open=lambda *args: 8, fstat=lambda fd: inode,
            ftruncate=lambda fd, length: state.update(value=""),
            fdopen=broken_stream, close=lambda fd: state.update(closed=state["closed"]+1),
            path=SimpleNamespace(samestat=lambda left, right: left is right))
        private = inert_module("skills/schedule-reminder/scripts/private_data.py",
            os=fake_os, prepare_parent=lambda path: None, prove_private=lambda path: None,
            assert_writable_path=lambda value: path)
        with self.assertRaises(OSError):
            private.open_for_write("synthetic-record", "w")
        self.assertEqual(state["value"], original)
        self.assertEqual(state["closed"], 1)

    def test_manual_arm_rejects_incomplete_reactor_snapshots(self):
        for failure in ("fetch", "full-page"):
            with self.subTest(failure=failure):
                world = channel_world()
                namespace = world.ingest.arm_reactions.__globals__
                def reactors(*args, strict=False, **kwargs):
                    if strict:
                        raise RuntimeError(case()["failure"])
                    return []
                namespace["_reactors"] = reactors
                with self.assertRaises(RuntimeError):
                    world.ingest.arm_reactions(world.registry, world.case["token"])
                self.assertNotIn(world.key(world.ingest._seen_file(
                    world.ingest._key(world.stream, world.case["channel"]))), world.files)

    def test_manual_arm_complete_snapshot_is_saved(self):
        world = channel_world()
        self.assertEqual(world.ingest.arm_reactions(world.registry, world.case["token"]), 1)
        seen = world.ingest._load_seen(world.ingest._key(world.stream, world.case["channel"]))
        self.assertEqual(len(seen), 1)

    def test_stop_recovers_cancelled_authorized_work_without_repeating_stop(self):
        fixture = case()
        for generation in (0, 1):
            with self.subTest(generation=generation):
                stopped, reads = [], []
                item = {"id": fixture["work_id"], "state": "cancelled",
                        "ext": {"x_agent_exec_state": "failed"}}
                operation = None if generation == 0 else {
                    "generation": generation, "outcome": "cancelled", "cleanup_state": "quiescent"}
                task = SimpleNamespace(get=lambda iid: reads.append(iid) or item,
                    operation=lambda iid: operation,
                    exec_state=lambda row: (row.get("ext") or {}).get("x_agent_exec_state"),
                    STATE_FAILED="failed")
                dispatch = inert_module("skills/schedule-reminder/scripts/dispatch.py",
                    _action_identity=lambda *args: "synthetic-action",
                    agent_task=task, agent_tick=SimpleNamespace(stop=lambda *args, **kwargs: stopped.append(args)))
                for selected in (fixture["work_id"], "*"):
                    result = dispatch.execute(fixture["stream"], {"kind": "reminder"},
                        {"actions": [{"op": "stop", "id": selected}]}, [],
                        work=[], authorized_work_ids=[fixture["work_id"]],
                        authorized_work_generations={fixture["work_id"]: generation})
                    self.assertEqual(result["stopped"], [fixture["work_id"]])
                    self.assertEqual(result["failed"], [])
                self.assertEqual(stopped, [])
                self.assertEqual(reads, [fixture["work_id"], fixture["work_id"]])

    def test_stop_without_saved_generation_cannot_read_or_stop_work(self):
        fixture = case()
        effects = []
        dispatch = inert_module("skills/schedule-reminder/scripts/dispatch.py",
            _action_identity=lambda *args: "synthetic-action",
            agent_task=SimpleNamespace(get=lambda *args: effects.append("get"),
                                       operation=lambda *args: effects.append("operation")),
            agent_tick=SimpleNamespace(stop=lambda *args, **kwargs: effects.append("stop") or []))
        for generations in (None, {}, {fixture["work_id"]: None}, {fixture["work_id"]: True}):
            with self.subTest(generations=generations):
                result = dispatch.execute(fixture["stream"], {"kind": "reminder"},
                    {"actions": [{"op": "stop", "id": fixture["work_id"]}]}, [],
                    work=[{"id": fixture["work_id"]}], authorized_work_ids=[fixture["work_id"]],
                    authorized_work_generations=generations)
                self.assertEqual(result["stopped"], [])
                self.assertTrue(result["failed"])
                self.assertEqual(effects, [])

    def test_stop_recovery_cannot_read_or_stop_unshown_work(self):
        fixture = case()
        effects = []
        dispatch = inert_module("skills/schedule-reminder/scripts/dispatch.py",
            _action_identity=lambda *args: "synthetic-action",
            agent_task=SimpleNamespace(get=lambda *args: effects.append("get")),
            agent_tick=SimpleNamespace(stop=lambda *args, **kwargs: effects.append("stop") or []))
        result = dispatch.execute(fixture["stream"], {"kind": "reminder"},
            {"actions": [{"op": "stop", "id": fixture["other_work_id"]}]}, [],
            work=[], authorized_work_ids=[fixture["work_id"]],
            authorized_work_generations={fixture["work_id"]: 1})
        self.assertEqual(result["stopped"], [])
        self.assertTrue(result["skipped"])
        self.assertEqual(effects, [])

    def test_cancel_state_and_metadata_share_one_transaction(self):
        store, connection = storage()
        try:
            item = store.add_item(case()["title"], state="doing",
                ext={"x_agent_exec_state": "stop_pending", **case()["preserved_ext"]})
            calls = []
            def rem(*argv):
                calls.append(argv)
                if argv[0] != "transition":
                    return {"_err": "synthetic forbidden second transaction"}
                ext = json.loads(argv[argv.index("--ext")+1]) if "--ext" in argv else None
                return {"item": store.transition(item["id"], "cancelled", ext=ext)}
            task = inert_module("skills/schedule-reminder/scripts/agent_task.py", rem=rem)
            result = task.cancel(item["id"], case()["failure"])
            self.assertNotIn("_err", result)
            saved = store.get_item(item["id"])
            self.assertEqual(saved["state"], "cancelled")
            self.assertEqual(saved["ext"]["x_agent_exec_state"], "failed")
            self.assertEqual(saved["ext"]["x_synthetic_existing"], "keep")
            self.assertEqual(len(calls), 1)
        finally:
            connection.close()

    def test_cancel_write_failure_leaves_active_state_and_metadata(self):
        store, connection = storage()
        try:
            item = store.add_item(case()["title"], state="doing",
                ext={"x_agent_exec_state": "stop_pending", **case()["preserved_ext"]})
            before = (store.get_item(item["id"]), list(connection.execute("SELECT * FROM events")))
            original = store.transition.__globals__["_append_event"]
            def fail_event(*args, **kwargs):
                raise OSError(case()["failure"])
            store.transition.__globals__["_append_event"] = fail_event
            def rem(*argv):
                try:
                    ext = json.loads(argv[argv.index("--ext")+1]) if "--ext" in argv else None
                    return {"item": store.transition(item["id"], "cancelled", ext=ext)}
                except OSError:
                    return {"_err": case()["failure"]}
            task = inert_module("skills/schedule-reminder/scripts/agent_task.py", rem=rem)
            self.assertIn("_err", task.cancel(item["id"]))
            self.assertEqual((store.get_item(item["id"]), list(connection.execute("SELECT * FROM events"))), before)
            store.transition.__globals__["_append_event"] = original
            self.assertNotIn("_err", task.cancel(item["id"]))
            current = store.get_item(item["id"])
            self.assertEqual((current["state"], current["ext"]["x_agent_exec_state"]), ("cancelled", "failed"))
        finally:
            connection.close()

    def test_stop_outcome_save_failure_reconciles_on_retry(self):
        fixture = case()
        current = {"id": fixture["work_id"], "state": "doing", "ext": {"x_agent_exec_state": "running"}}
        operation = {"generation": 1, "outcome": None, "cleanup_state": "quiescent"}
        stops = []
        def stop(identity, **kwargs):
            self.assertEqual(kwargs["expected_generation"], operation["generation"])
            stops.append(identity)
            current.update(state="cancelled", ext={"x_agent_exec_state": "failed"})
            operation.update(outcome="cancelled")
            return [{"id": identity, "stopped": True}]
        def fail_save(*args):
            raise OSError(fixture["failure"])
        task = SimpleNamespace(get=lambda identity: copy.deepcopy(current),
            operation=lambda identity: copy.deepcopy(operation),
            exec_state=lambda item: item["ext"]["x_agent_exec_state"], STATE_FAILED="failed")
        dispatch = inert_module("skills/schedule-reminder/scripts/dispatch.py",
            _action_identity=lambda *args: "synthetic-action", agent_task=task,
            agent_tick=SimpleNamespace(stop=stop))
        plan = {"actions": [{"op": "stop", "id": fixture["work_id"]}]}
        with self.assertRaises(OSError):
            dispatch.execute(fixture["stream"], {"kind": "reminder"}, plan, [],
                work=[copy.deepcopy(current)], authorized_work_ids=[fixture["work_id"]],
                authorized_work_generations={fixture["work_id"]: 1},
                save_outcome=fail_save)
        result = dispatch.execute(fixture["stream"], {"kind": "reminder"}, plan, [],
            work=[], authorized_work_ids=[fixture["work_id"]], saved_outcomes={},
            authorized_work_generations={fixture["work_id"]: 1})
        self.assertEqual(result["stopped"], [fixture["work_id"]])
        self.assertEqual(stops, [fixture["work_id"]])

    def test_shared_https_transport_refusal_stops_writes(self):
        fixture = case()
        for config in fixture["http_config_overrides"]:
            with self.subTest(config=config[0]):
                self._private_transport(config, {})
        for key, value in fixture["http_environment_overrides"]:
            with self.subTest(environment=key):
                self._private_transport(None, {key: value})

    def _private_transport(self, config, environment, reject=True):
        """Generated transport outcomes verify adapter propagation, not Guards policy."""
        boundary = offline_support.synthetic_boundary()
        calls = []
        with tempfile.TemporaryDirectory(prefix="schedule-transport-") as directory:
            root = Path(directory)
            target = root/"pending"/"record.json"
            def prove(destination):
                calls.append(("proof", str(destination)))
                if reject:
                    raise boundary.GitError("synthetic shared transport refusal")
                return SimpleNamespace(root=str(root), repositories=("example-owner/private-data",),
                                       signature="synthetic-transport")
            def query(argv):
                self.assertEqual(argv[:3], ["gh", "repo", "view"])
                calls.append(("visibility", argv[3]))
                return json.dumps({"nameWithOwner": argv[3], "visibility": "PRIVATE"})
            boundary.prove_private_companion = prove
            with patch.object(private_data, "_shared_boundary", return_value=boundary), \
                    patch.object(private_data, "_query", side_effect=query), \
                    patch.dict(private_data.os.environ, environment, clear=True):
                if reject:
                    with self.assertRaises(ValueError):
                        private_data.prepare_parent(target)
                    self.assertEqual(calls, [("proof", str(root))])
                    self.assertFalse(target.parent.exists())
                else:
                    self.assertEqual(private_data.prove_private(target)["repositories"],
                                     ["example-owner/private-data"])
                    self.assertEqual(calls, [("proof", str(root)),
                                            ("visibility", "example-owner/private-data"),
                                            ("proof", str(root))])

    def test_shared_https_private_proof_is_admitted(self):
        self._private_transport(None, {}, reject=False)

    def test_invalid_priority_cannot_insert_or_update(self):
        for priority in case()["invalid_priorities"]:
            with self.subTest(priority=priority):
                store, connection = storage()
                try:
                    with self.assertRaises(store.SkillError):
                        store.add_item(case()["title"], priority=priority)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
                    item = store.add_item(case()["title"], priority=1)
                    before = dict(store.get_item(item["id"]))
                    with self.assertRaises(store.SkillError):
                        store.update_item(item["id"], priority=priority)
                    self.assertEqual(store.get_item(item["id"]), before)
                finally:
                    connection.close()

    def test_priority_boundaries_and_integer_text_are_preserved(self):
        for priority in case()["valid_priorities"]:
            with self.subTest(priority=priority):
                store, connection = storage()
                try:
                    item = store.add_item(case()["title"], priority=priority)
                    self.assertEqual(item["priority"], int(priority))
                    self.assertEqual(store.update_item(item["id"], priority=priority)["priority"], int(priority))
                finally:
                    connection.close()

    def test_blank_block_reason_is_rejected_without_poisoning_item(self):
        for reason in case()["blank_reasons"]:
            with self.subTest(reason=reason):
                store, connection = storage()
                try:
                    item = store.add_item(case()["title"])
                    with self.assertRaises(store.SkillError):
                        store.block(item["id"], reason=reason)
                    self.assertEqual(store.get_item(item["id"])["state"], "pending")
                    self.assertEqual(store.update_item(item["id"], priority=1)["priority"], 1)
                finally:
                    connection.close()

    def test_nonblank_block_reason_allows_later_update(self):
        store, connection = storage()
        try:
            item = store.add_item(case()["title"])
            store.block(item["id"], reason=case()["failure"])
            self.assertEqual(store.update_item(item["id"], priority=1)["state"], "blocked")
        finally:
            connection.close()

    def test_corrupt_digest_is_failure_before_contributors_or_delivery(self):
        digest = inert_module("skills/schedule-reminder/scripts/digest.py",
            _path=lambda: "synthetic-digest", open=lambda *args, **kwargs: io.StringIO(case()["bad_digest"]),
            _run_contributor=support.forbidden, _relay=support.forbidden)
        with self.assertRaises(ValueError):
            digest._assemble(None)

    def test_corrupt_digest_cli_reports_failure_without_empty_roster(self):
        for operation in ("run", "collect", "list"):
            with self.subTest(operation=operation):
                digest = inert_module("skills/schedule-reminder/scripts/digest.py",
                    argparse=argparse, _path=lambda: "synthetic-digest",
                    open=lambda *args, **kwargs: io.StringIO(case()["bad_digest"]),
                    _run_contributor=support.forbidden, _relay=support.forbidden)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(digest.main([operation]), 1)
                report = json.loads(output.getvalue())
                self.assertFalse(report["ok"])
                self.assertEqual(report["error_code"], "ERR_DIGEST_CONFIG")
                self.assertNotIn(case()["bad_digest"], output.getvalue())

    def test_missing_digest_remains_explicit_empty_roster(self):
        def absent(*args, **kwargs):
            raise FileNotFoundError
        digest = inert_module("skills/schedule-reminder/scripts/digest.py",
            _path=lambda: "synthetic-digest", open=absent,
            _run_contributor=support.forbidden, _relay=support.forbidden)
        self.assertEqual(digest._assemble(None)[3]["registered"], 0)

    def test_hook_targets_require_regular_nonempty_programs(self):
        for hook in ("pre-commit", "pre-push"):
            with self.subTest(hook=hook):
                source = support.source_text(".githooks/" + hook)
                condition = next(line for line in source.splitlines() if line.startswith("if [ ! -"))
                # Model the exact shipped POSIX predicate. Native execution is a separate gate.
                for regular, nonempty, expected in ((False, False, True), (True, False, True),
                                                    (False, True, True), (True, True, False)):
                    if '[ ! -f "$_REAL" ] || [ ! -s "$_REAL" ]' in condition:
                        blocked = not regular or not nonempty
                    elif '[ ! -x "$_REAL" ] && [ ! -f "$_REAL" ]' in condition:
                        blocked = not regular
                    else:
                        self.fail("Unreviewed forwarding predicate")
                    self.assertEqual(blocked, expected)

    def test_extension_contract_describes_shallow_merge(self):
        doc = ast.get_docstring(next(node for node in ast.parse(support.source_text(
              "skills/schedule-reminder/scripts/store.py")).body
              if isinstance(node, ast.FunctionDef) and node.name == "update_item"))
        self.assertIn("shallow", doc.lower())
        self.assertNotIn("deep-merged", doc)


if __name__ == "__main__":
    unittest.main()
