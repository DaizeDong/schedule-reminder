"""Read-only reminder linkage seam, gated by an explicit owner review receipt.

No store import, discovery, database creation or migration occurs on reads.
See ../reference/linkage-review.md for the private review/apply workflow.
"""
import hashlib
import json
from pathlib import Path
import sqlite3


REVIEW_KEY = "task_console_link_review"
MARKER_KEY = "task_console_link_schema"
MAX_ITEMS = 100000


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _revision(value):
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_number(value):
    raise ValueError("invalid JSON number")


def _loads(raw):
    return json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid_number)


def _database(path):
    path = Path(path)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("existing absolute database path required")
    path = path.resolve(strict=True)
    stat = path.stat()
    return {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino}


def _schema(conn):
    return _revision({"user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "objects": [list(row) for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]})


def _items(conn):
    result = []
    for item_id, version, state, raw in conn.execute("SELECT id,schema_version,state,ext FROM items ORDER BY id"):
        if len(result) >= MAX_ITEMS:
            raise ValueError("linked reader limit")
        ext = _loads(raw) if raw not in (None, "") else {}
        if not isinstance(ext, dict):
            raise ValueError("invalid work-item extension")
        link = ext.get("task_console")
        if "task_console" in ext and (not isinstance(link, dict)
                or not isinstance(link.get("task_id"), str) or not link["task_id"].strip()):
            raise ValueError("invalid work-item task link")
        result.append({"id": item_id, "version": version, "state": state, "raw": raw,
                       "ext": ext, "link": link})
    return result


def _binding(conn, path, items):
    return {"database": _database(path), "schema": _schema(conn),
            "coverage": "explicit-links-v1",
            "linkage": _revision([[row["id"], row["version"], row["link"]]
                                  for row in items if row["link"] is not None])}


def _meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _review_revision(review):
    return _revision({key: value for key, value in review.items() if key != "review_revision"})


def _valid_receipt(receipt, binding, items):
    if not isinstance(receipt, dict) or type(receipt.get("schemaVersion")) is not int or receipt["schemaVersion"] != 1:
        return False
    review = receipt.get("review")
    if not isinstance(review, dict) or review.get("review_revision") != _review_revision(review):
        return False
    after = receipt.get("after")
    if (review.get("scope") != "ext.task_console.task_id" or type(review.get("schemaVersion")) is not int
            or review["schemaVersion"] != 1 or not isinstance(after, dict) or after.get("binding") != binding):
        return False
    decisions = review.get("decisions", {})
    if not isinstance(decisions, dict):
        return False
    links = decisions.get("links")
    if decisions.get("unmapped_items") != "reviewed-unlinked" or not isinstance(links, dict):
        return False
    known = review.get("known_task_ids")
    if not isinstance(known, list) or any(not isinstance(value, str) for value in known):
        return False
    actual = {row["id"]: row["link"]["task_id"] for row in items if row["link"] is not None}
    return (actual == links and all(value in known for value in actual.values())
            and type(review.get("item_count")) is int and review["item_count"] >= len(actual)
            and review.get("linked_count") == len(actual)
            and review.get("unlinked_count") == review["item_count"] - len(actual))


def read_linked_items(task_ids, *, db_path):
    if (not isinstance(task_ids, list) or any(not isinstance(i, str) or not i.strip() for i in task_ids)
            or len(set(task_ids)) != len(task_ids)):
        raise ValueError("explicit unique task IDs required")
    path = Path(db_path)
    if not path.is_absolute():
        return {"status": "unavailable", "items": None, "code": "linked_db_not_configured"}
    if not path.is_file():
        return {"status": "unavailable", "items": None, "code": "linked_db_missing"}
    conn = None
    unavailable = {"status": "unavailable", "items": None, "code": "linked_schema_unreviewed"}
    try:
        identity = _database(path)
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if _meta(conn, MARKER_KEY) != "1":
            return unavailable
        raw = _meta(conn, REVIEW_KEY)
        if raw is None:
            return unavailable
        rows = _items(conn)
        binding = _binding(conn, path, rows)
        if identity != binding["database"] or not _valid_receipt(_loads(raw), binding, rows):
            return unavailable
        items = {i: [] for i in task_ids}
        for row in rows:
            link = row["link"]
            if link is not None and row["state"] not in ("done", "cancelled") and link["task_id"] in items:
                items[link["task_id"]].append({"id": row["id"], "state": row["state"]})
        return {"status": "available", "items": items}
    except (sqlite3.Error, ValueError, TypeError, AttributeError, OSError):
        return {"status": "error", "items": None, "code": "linked_reader_failed"}
    finally:
        if conn is not None:
            conn.close()
