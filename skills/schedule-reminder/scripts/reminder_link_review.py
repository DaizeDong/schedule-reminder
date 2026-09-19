"""Explicit reminder-owner linkage review; no default DB, backup or task writes."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys

import reminder_linked_items as linked


def _known_tasks(task_request):
    if task_request is None:
        return []
    # Reuse the task owner's authority checks and compiler, including installed IDs.
    # This pure function does not instantiate a runtime or read a task database.
    from task_console.registration import _compile
    return sorted(row["task_id"] for row in _compile({"request": task_request})["task_specs"])


def _content(conn):
    """Hash all logical DB rows, excluding only this receipt to avoid self-reference."""
    tables = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        quoted = '"' + name.replace('"', '""') + '"'
        sql, args = "SELECT * FROM " + quoted, ()
        if name == "meta":
            sql += " WHERE key != ?"
            args = (linked.REVIEW_KEY,)
        rows = [[{"bytes": value.hex()} if isinstance(value, bytes) else value for value in row]
                for row in conn.execute(sql, args)]
        tables.append([name, sorted(rows, key=linked._json)])
    return linked._revision(tables)


def _baseline(conn, path, items):
    return {"binding": linked._binding(conn, path, items), "content": _content(conn),
            "prior_review": linked._revision(linked._meta(conn, linked.REVIEW_KEY))}


def _validate_decisions(decisions, items, known):
    if (not isinstance(decisions, dict) or set(decisions) != {"schemaVersion", "links", "unmapped_items"}
            or type(decisions["schemaVersion"]) is not int or decisions["schemaVersion"] != 1
            or decisions["unmapped_items"] != "reviewed-unlinked" or not isinstance(decisions["links"], dict)):
        raise ValueError("explicit complete linkage review required")
    links = decisions["links"]
    by_id = {row["id"]: row for row in items}
    for item_id, task_id in links.items():
        if (not isinstance(item_id, str) or item_id not in by_id
                or not isinstance(task_id, str) or task_id not in known):
            raise ValueError("unknown item or task ID in reviewed mapping")
    for row in items:
        if row["link"] is not None:
            task_id = row["link"]["task_id"]
            if task_id not in known or links.get(row["id"]) != task_id:
                raise ValueError("existing linkage must be valid, explicit and unchanged")


def _plan(conn, path, decisions, task_request, known):
    import store
    if conn.execute("PRAGMA user_version").fetchone()[0] != store.SCHEMA_USER_VERSION:
        raise ValueError("run the existing reminder schema migration before reviewing linkage")
    items = linked._items(conn)
    _validate_decisions(decisions, items, known)
    review = {"schemaVersion": 1, "scope": "ext.task_console.task_id",
              "baseline": _baseline(conn, path, items), "decisions": deepcopy(decisions),
              "known_task_ids": known, "task_input_revision": linked._revision(task_request),
              "item_count": len(items), "linked_count": len(decisions["links"]),
              "unlinked_count": len(items) - len(decisions["links"]),
              "existing_link_count": sum(row["link"] is not None for row in items)}
    review["review_revision"] = linked._review_revision(review)
    return review


def build_review(*, db_path, decisions, task_request=None):
    """Return a private review document from a read-only snapshot. Never initialize."""
    decisions, task_request = deepcopy(decisions), deepcopy(task_request)
    identity = linked._database(db_path)
    path = Path(identity["path"])
    known = _known_tasks(task_request)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        review = _plan(conn, path, decisions, task_request, known)
        if review["baseline"]["binding"]["database"] != identity:
            raise ValueError("database replaced during review")
        return review
    finally:
        conn.close()


def apply_review(review, approval_revision, *, db_path, task_request=None):
    """Apply the exact approved review under the owner's transaction discipline."""
    import store
    if (not isinstance(review, dict) or approval_revision != review.get("review_revision")
            or approval_revision != linked._review_revision(review)):
        raise ValueError("exact review revision approval required")
    review = deepcopy(review)
    task_request = deepcopy(task_request)
    known = _known_tasks(task_request)
    if review.get("task_input_revision") != linked._revision(task_request) or review.get("known_task_ids") != known:
        raise ValueError("reviewed task input changed")
    identity = linked._database(db_path)
    path = Path(identity["path"])
    # mode=rw must fail for missing DBs; store._connect intentionally creates DBs.
    conn = store.sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=10)
    try:
        with store._Tx(conn):
            items = linked._items(conn)
            binding = linked._binding(conn, path, items)
            if identity != binding["database"]:
                raise ValueError("database replaced during apply")
            raw = linked._meta(conn, linked.REVIEW_KEY)
            if raw is not None:
                receipt = linked._loads(raw)
                if (linked._valid_receipt(receipt, binding, items)
                        and receipt["review"] == review and linked._meta(conn, linked.MARKER_KEY) == "1"
                        and receipt["after"]["content"] == _content(conn)):
                    return {"status": "already_reviewed", "review_revision": approval_revision,
                            "linked_count": review["linked_count"], "unlinked_count": review["unlinked_count"]}
            if _plan(conn, path, review["decisions"], task_request, known) != review:
                raise ValueError("database or schema changed since review")
            for row in items:
                task_id = review["decisions"]["links"].get(row["id"])
                if task_id is not None and row["link"] is None:
                    ext = dict(row["ext"], task_console={"task_id": task_id})
                    cursor = conn.execute("UPDATE items SET ext=? WHERE id=? AND ext IS ?",
                                          (store._dump_json(ext), row["id"], row["raw"]))
                    if cursor.rowcount != 1:
                        raise ValueError("concurrent linkage edit")
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (linked.MARKER_KEY, "1"))
            after = {"binding": linked._binding(conn, path, linked._items(conn)), "content": _content(conn)}
            if after["binding"]["database"] != identity:
                raise ValueError("database replaced during apply")
            receipt = {"schemaVersion": 1, "review": review, "applied_at": store.to_rfc3339(store.now_utc()), "after": after}
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (linked.REVIEW_KEY, linked._json(receipt)))
        return {"status": "reviewed", "review_revision": approval_revision,
                "linked_count": review["linked_count"], "unlinked_count": review["unlinked_count"]}
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing absolute reminder database path")
    parser.add_argument("--task-input", help="Private task-console request JSON; required for any links")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("review", help="Read-only review document on stdout (private data)")
    prepare.add_argument("--mapping", required=True, help="Explicit reviewed decisions JSON")
    apply = sub.add_parser("apply", help="Apply one exact approved review")
    apply.add_argument("--review", required=True)
    apply.add_argument("--approve", required=True, help="Exact review_revision displayed during review")
    args = parser.parse_args(argv)
    try:
        def read(path):
            return linked._loads(Path(path).read_text(encoding="utf-8-sig"))
        request = read(args.task_input) if args.task_input else None
        if args.command == "review":
            result = build_review(db_path=args.db, decisions=read(args.mapping), task_request=request)
        else:
            result = apply_review(read(args.review), args.approve, db_path=args.db, task_request=request)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:
        # Never echo private mapping values, paths, or task bindings on failure.
        # This includes store.SkillError on lock exhaustion and optional SQLite backends.
        print(json.dumps({"status": "error", "code": "linkage_review_rejected", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
