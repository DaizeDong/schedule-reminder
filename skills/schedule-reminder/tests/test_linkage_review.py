"""Owner review regressions using generated Acme input and real SQLite only."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
RUN = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(SCRIPTS), str(RUN / "task-console/scripts")]
import store
import reminder_linked_items as reader


@pytest.fixture
def db(monkeypatch):
    # mkdir with inherited permissions avoids Windows pytest private-temp ACL issues.
    root = Path(tempfile.gettempdir()) / ("link-review-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    for name in ("HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(root))
    path = root / "synthetic.sqlite3"
    store.init_db(str(path))
    return path


@pytest.fixture
def owner():
    assert (SCRIPTS / "reminder_link_review.py").is_file(), "owner review path is missing"
    import reminder_link_review
    return reminder_link_review


@pytest.fixture
def task_request():
    spec = importlib.util.spec_from_file_location("acme_link_fixtures", RUN / "task-console/tools/make_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.example_request()


TASK = "acme-maintenance/sync"


def decisions(links=None):
    return {"schemaVersion": 1, "links": links or {}, "unmapped_items": "reviewed-unlinked"}


def rows(db, table):
    with sqlite3.connect(db) as conn:
        return conn.execute('SELECT * FROM "' + table + '" ORDER BY 1').fetchall()


def add(db, ext=None, **kw):
    return store.add_item("SYNTHETIC_LINK_REVIEW", ext=ext, db_path=str(db), **kw)["id"]


def test_bare_marker_is_not_a_review(db):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO meta VALUES ('task_console_link_schema', '1')")
    result = reader.read_linked_items([TASK], db_path=db)
    assert result == {"status": "unavailable", "items": None, "code": "linked_schema_unreviewed"}


def test_explicit_link_preserves_business_fields_and_history(db, owner, task_request):
    item = add(db, {"x_synthetic": {"keep": [1, 2]}})
    before, history = rows(db, "items"), rows(db, "events")
    review = owner.build_review(db_path=db, decisions=decisions({item: TASK}), task_request=task_request)
    result = owner.apply_review(review, review["review_revision"], db_path=db, task_request=task_request)
    assert result["status"] == "reviewed" and result["linked_count"] == 1
    after = rows(db, "items")
    assert [r[:-1] for r in after] == [r[:-1] for r in before]
    assert json.loads(after[0][-1]) == {"x_synthetic": {"keep": [1, 2]}, "task_console": {"task_id": TASK}}
    assert rows(db, "events") == history
    assert reader.read_linked_items([TASK], db_path=db) == {
        "status": "available", "items": {TASK: [{"id": item, "state": "pending"}]}}


def test_empty_mapping_is_explicit_review_and_repeat_is_noop(db, owner):
    add(db)
    before, history = rows(db, "items"), rows(db, "events")
    review = owner.build_review(db_path=db, decisions=decisions())
    first = owner.apply_review(review, review["review_revision"], db_path=db)
    meta = rows(db, "meta")
    second = owner.apply_review(review, review["review_revision"], db_path=db)
    assert first["linked_count"] == 0 and first["unlinked_count"] == 1
    assert second["status"] == "already_reviewed"
    assert rows(db, "items") == before and rows(db, "events") == history and rows(db, "meta") == meta
    assert reader.read_linked_items([TASK], db_path=db) == {"status": "available", "items": {TASK: []}}


@pytest.mark.parametrize("edit", ["item", "event", "schema", "new_item", "marker"])
def test_stale_approval_cannot_overwrite_edits(db, owner, edit):
    item = add(db)
    review = owner.build_review(db_path=db, decisions=decisions())
    with sqlite3.connect(db) as conn:
        if edit == "item":
            conn.execute("UPDATE items SET title='SYNTHETIC_EDIT' WHERE id=?", (item,))
        elif edit == "event":
            conn.execute("UPDATE events SET actor='synthetic-edit'")
        elif edit == "schema":
            conn.execute("CREATE INDEX synthetic_index ON items(title)")
        elif edit == "marker":
            conn.execute("INSERT INTO meta VALUES ('task_console_link_schema', '1')")
    if edit == "new_item":
        add(db)
    before = rows(db, "items"), rows(db, "events"), rows(db, "meta")
    with pytest.raises(ValueError):
        owner.apply_review(review, review["review_revision"], db_path=db)
    assert before == (rows(db, "items"), rows(db, "events"), rows(db, "meta"))


@pytest.mark.parametrize("raw", ['[]', '{bad', '{"task_console":null}', '{"task_console":{}}',
    '{"task_console":{"task_id":42}}', '{"task_console":{"task_id":"unknown/task"}}',
    '{"task_console":{"task_id":"acme-maintenance/sync","task_id":"unknown/task"}}'])
def test_malformed_or_unknown_existing_links_block_review(db, owner, task_request, raw):
    item = add(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE items SET ext=? WHERE id=?", (raw, item))
    with pytest.raises(ValueError):
        owner.build_review(db_path=db, decisions=decisions(), task_request=task_request)
    assert rows(db, "meta") == []


def test_existing_links_must_be_explicit_and_cannot_be_reassigned(db, owner, task_request):
    item = add(db, {"task_console": {"task_id": TASK, "x_synthetic": "keep"}}, state="done")
    for mapping in ({}, {item: "unknown/task"}):
        with pytest.raises(ValueError):
            owner.build_review(db_path=db, decisions=decisions(mapping), task_request=task_request)
    before = rows(db, "items")
    review = owner.build_review(db_path=db, decisions=decisions({item: TASK}), task_request=task_request)
    owner.apply_review(review, review["review_revision"], db_path=db, task_request=task_request)
    assert rows(db, "items") == before
    assert reader.read_linked_items([TASK], db_path=db)["items"] == {TASK: []}


@pytest.mark.parametrize("mapping", [{"missing-item": TASK}, {"item": "unknown/task"}, {"item": ""}, {"item": 4}])
def test_invalid_mapping_is_rejected(db, owner, task_request, mapping):
    item = add(db)
    mapping = {item if key == "item" else key: value for key, value in mapping.items()}
    with pytest.raises(ValueError):
        owner.build_review(db_path=db, decisions=decisions(mapping), task_request=task_request)


def test_approval_binds_task_input_and_review_document(db, owner, task_request):
    item = add(db)
    review = owner.build_review(db_path=db, decisions=decisions({item: TASK}), task_request=task_request)
    changed = deepcopy(task_request)
    changed["bindings"]["tasks"][TASK]["enabled"] = True
    with pytest.raises(ValueError):
        owner.apply_review(review, review["review_revision"], db_path=db, task_request=changed)
    altered = deepcopy(review)
    altered["decisions"] = decisions()
    with pytest.raises(ValueError):
        owner.apply_review(altered, review["review_revision"], db_path=db, task_request=task_request)


def test_read_does_not_create_missing_or_migrate_legacy(db, owner):
    missing = db.parent / "absent" / "missing.sqlite3"
    assert reader.read_linked_items([TASK], db_path=missing)["code"] == "linked_db_missing"
    with pytest.raises((ValueError, OSError)):
        owner.build_review(db_path=missing, decisions=decisions())
    assert not missing.parent.exists()
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=1")
    assert reader.read_linked_items([TASK], db_path=db)["status"] == "unavailable"
    with pytest.raises(ValueError):
        owner.build_review(db_path=db, decisions=decisions())
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_linkage_drift_invalidates_read_but_business_state_change_does_not(db, owner, task_request):
    item = add(db)
    review = owner.build_review(db_path=db, decisions=decisions({item: TASK}), task_request=task_request)
    owner.apply_review(review, review["review_revision"], db_path=db, task_request=task_request)
    store.done(item, db_path=str(db))
    assert reader.read_linked_items([TASK], db_path=db) == {"status": "available", "items": {TASK: []}}
    add(db)
    assert reader.read_linked_items([TASK], db_path=db) == {"status": "available", "items": {TASK: []}}
    add(db, {"task_console": {"task_id": TASK}})
    assert reader.read_linked_items([TASK], db_path=db)["status"] == "unavailable"


def test_unlinked_item_churn_keeps_coverage_but_apply_still_requires_fresh_review(db, owner):
    old = add(db)
    review = owner.build_review(db_path=db, decisions=decisions())
    owner.apply_review(review, review["review_revision"], db_path=db)
    add(db)
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM items WHERE id=?', (old,))
    assert reader.read_linked_items([TASK], db_path=db) == {"status":"available", "items":{TASK:[]}}
    with pytest.raises(ValueError):
        owner.apply_review(review, review["review_revision"], db_path=db)
    add(db, {"task_console": {"task_id": "unreviewed/task"}})
    assert reader.read_linked_items([TASK], db_path=db)["status"] == "unavailable"


def test_concurrent_writer_commits_before_apply_lock(db, owner):
    item = add(db)
    review = owner.build_review(db_path=db, decisions=decisions())
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE items SET title='SYNTHETIC_CONCURRENT' WHERE id=?", (item,))
    entered, finished, errors = threading.Event(), threading.Event(), []

    def apply():
        entered.set()
        try:
            owner.apply_review(review, review["review_revision"], db_path=db)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=apply)
    thread.start()
    assert entered.wait(3)
    assert not finished.wait(0.1), "apply must wait for the real SQLite writer lock"
    writer.execute("COMMIT")
    writer.close()
    thread.join(5)
    assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], ValueError)
    assert rows(db, "meta") == []
    assert store.get_item(item, db_path=str(db))["title"] == "SYNTHETIC_CONCURRENT"


def test_transaction_rolls_back_links_when_attestation_fails(db, owner, task_request):
    item = add(db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TRIGGER synthetic_fail BEFORE INSERT ON meta BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    before = rows(db, "items"), rows(db, "events")
    review = owner.build_review(db_path=db, decisions=decisions({item: TASK}), task_request=task_request)
    with pytest.raises(sqlite3.Error):
        owner.apply_review(review, review["review_revision"], db_path=db, task_request=task_request)
    assert before == (rows(db, "items"), rows(db, "events")) and rows(db, "meta") == []


def test_exact_database_identity_rejects_identical_copy(db, owner):
    review = owner.build_review(db_path=db, decisions=decisions())
    target = db.parent / "other.sqlite3"
    source = sqlite3.connect(db)
    dest = sqlite3.connect(target)
    source.backup(dest)
    source.close()
    dest.close()
    with pytest.raises(ValueError):
        owner.apply_review(review, review["review_revision"], db_path=target)


def test_link_conflict_even_when_both_ids_are_valid(db, owner, task_request):
    second = "acme-maintenance/other"
    task_request["components"][0]["tasks"].append(dict(task_request["components"][0]["tasks"][0], id="other"))
    task_request["bindings"]["tasks"][second] = dict(task_request["bindings"]["tasks"][TASK], name="AcmeOther")
    item = add(db, {"task_console": {"task_id": TASK}})
    with pytest.raises(ValueError):
        owner.build_review(db_path=db, decisions=decisions({item: second}), task_request=task_request)


def test_cli_review_apply_and_private_error_output(db, owner):
    script = SCRIPTS / "reminder_link_review.py"
    mapping, plan = db.parent / "mapping.json", db.parent / "review.json"
    mapping.write_text(json.dumps(decisions()), encoding="utf-8")
    command = [sys.executable, str(script), "--db", str(db)]
    prepared = subprocess.run(command + ["review", "--mapping", str(mapping)], capture_output=True, text=True)
    assert prepared.returncode == 0, prepared.stderr
    plan.write_text(prepared.stdout, encoding="utf-8")
    revision = json.loads(prepared.stdout)["review_revision"]
    result = subprocess.run(command + ["apply", "--review", str(plan), "--approve", revision], capture_output=True, text=True)
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "reviewed"
    mapping.write_text('{"links":{},"links":{"SYNTHETIC_PRIVATE":"SECRET"}}', encoding="utf-8")
    invalid = subprocess.run(command + ["review", "--mapping", str(mapping)], capture_output=True, text=True)
    assert invalid.returncode == 1 and "SYNTHETIC_PRIVATE" not in invalid.stderr and "SECRET" not in invalid.stderr


def test_task_console_seam_uses_owner_attestation(db, owner, task_request, monkeypatch):
    from task_console import linked_items
    item = add(db)
    review = owner.build_review(db_path=db, decisions=decisions({item: TASK}), task_request=task_request)
    owner.apply_review(review, review["review_revision"], db_path=db, task_request=task_request)
    monkeypatch.setenv("TASK_CONSOLE_REMINDER_DB", str(db))
    assert linked_items.read_linked_items([TASK]) == {"status": "available", "items": {TASK: [{"id": item, "state": "pending"}]}}


def test_installed_namespaced_id_comes_from_task_console(db, owner):
    spec = importlib.util.spec_from_file_location("acme_installed_fixtures", RUN / "task-console/tools/make_fixtures.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    request = fixture.installation_request()
    task = "acme-user/acme-maintenance/sync"
    request["machine"]["migrated_tasks"] = [task]
    item = add(db)
    review = owner.build_review(db_path=db, decisions=decisions({item: task}), task_request=request)
    owner.apply_review(review, review["review_revision"], db_path=db, task_request=request)
    assert reader.read_linked_items([task], db_path=db)["items"][task] == [{"id": item, "state": "pending"}]


def test_reader_in_fresh_process_never_imports_store(db, owner):
    review = owner.build_review(db_path=db, decisions=decisions())
    owner.apply_review(review, review["review_revision"], db_path=db)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import reminder_linked_items as r; "
            "assert 'store' not in sys.modules; assert r.read_linked_items([], db_path=sys.argv[2])"
            " == {'status':'available','items':{}}; assert 'store' not in sys.modules")
    result = subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(db)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
