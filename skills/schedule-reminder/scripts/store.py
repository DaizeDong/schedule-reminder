#!/usr/bin/env python3
"""schedule-reminder — storage layer (the regulated implementation).

This is the *private* engine. Trusted, in-process skills MAY import it; everything else MUST go
through the frozen CLI contract (reminder.py). It owns SQL, transaction boundaries, the state
machine, write-time invariants, the events audit stream, idempotent upsert, BUSY back-off and the
due/tick reconciliation. See ../reference/contract.md for the stable surface.

Design invariants enforced here (T0 base — these are the promise to downstream skills):
  * every write = BEGIN IMMEDIATE short transaction (never default DEFERRED -> instant SQLITE_BUSY)
  * conditional UPDATE ... WHERE state=expected = optimistic CAS (rowcount 0 => explicit conflict)
  * one in-process write lock serialises same-process writers; SQLite file lock handles cross-process
  * bounded exponential back-off on SQLITE_BUSY/locked
  * unknown record fields are MUST-PRESERVE (round-tripped through the `ext` JSON column)
  * all timestamps are UTC RFC3339 with fixed microsecond precision (lexical order == time order)

Stdlib only (sqlite3, uuid, json, threading, time, os, datetime). Optional: pysqlite3 (newer SQLite).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid as _uuid
from datetime import datetime, timezone

# --- SQLite backend selection: prefer a bundled-newer pysqlite3 if present, else stdlib ----------
try:  # pragma: no cover - depends on host
    import pysqlite3 as sqlite3  # type: ignore
    _SQLITE_BACKEND = "pysqlite3"
except Exception:  # pragma: no cover
    import sqlite3  # type: ignore
    _SQLITE_BACKEND = "stdlib"

# =================================================================================================
# Versions / contract constants
# =================================================================================================
API_VERSION = "1.0.0"          # external CLI/JSON contract version (decoupled from DB user_version)
SCHEMA_USER_VERSION = 1        # PRAGMA user_version target (additive migrations only)
RECORD_SCHEMA_VERSION = 1      # per-row schema_version (tolerant forward parsing)
RECOMMENDED_SQLITE = "3.51.3"  # < this => known WAL-reset multi-writer corruption bug (advisory)

STATES = ("pending", "doing", "done", "blocked", "cancelled")
ACTIVE_STATES = ("pending", "doing", "blocked")
TERMINAL_STATES = ("done", "cancelled")

# Legal state-machine transitions (architecture section 2.3). Reopen from terminal allowed.
TRANSITIONS = {
    "pending":   {"doing", "blocked", "done", "cancelled"},
    "doing":     {"done", "blocked", "pending", "cancelled"},
    "blocked":   {"doing", "pending", "done", "cancelled"},
    "done":      {"pending"},       # terminal, protected (reopen only)
    "cancelled": {"pending"},       # soft-deleted terminal, protected (reopen only)
}

KINDS = ("event", "task")

# Columns stored as JSON text and auto-decoded on read.
_JSON_COLS = ("tags", "rdate", "exdate", "relations", "alarms", "ext")

# Full item column set, frozen for backward compat (add columns only; never rename/drop/retype).
ITEM_COLUMNS = (
    "id", "schema_version", "kind", "title", "description", "state", "progress", "priority",
    "due_at", "scheduled_at", "start_at", "end_at", "wait_until", "tz", "recurrence",
    "rdate", "exdate", "tags", "project", "relations", "alarms", "source",
    "idempotency_key", "notified_at", "next_retry_at", "retry_count", "claimed_at",
    "block_reason", "created_at", "updated_at", "ext",
)

# BUSY back-off
_BUSY_MAX_RETRIES = 6
_BUSY_BASE = 0.05  # seconds
# notify retry back-off (tick)
_NOTIFY_MAX_RETRIES = 5
_NOTIFY_BACKOFF_BASE = 60      # seconds
_NOTIFY_BACKOFF_CAP = 30 * 60  # 30 min

_WRITE_LOCK = threading.RLock()  # serialises same-process writers


# =================================================================================================
# Errors
# =================================================================================================
class SkillError(Exception):
    """Structured, machine-readable error. Never silently swallowed."""

    def __init__(self, error_code, message, **extra):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.extra = extra

    def to_dict(self):
        d = {"error_code": self.error_code, "message": self.message}
        d.update(self.extra)
        return d


# =================================================================================================
# Time helpers — all UTC RFC3339, fixed microsecond precision so string order == time order
# =================================================================================================
def now_utc():
    return datetime.now(timezone.utc)


def to_rfc3339(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_dt(s):
    """Parse an RFC3339/ISO string (accepts trailing Z) to an aware UTC datetime."""
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise SkillError("ERR_BAD_TIME", "not an RFC3339 timestamp: %r" % s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve_now(now=None):
    """now precedence: explicit arg -> SCHEDULE_NOW env -> wall clock. Returns RFC3339 string."""
    if now is not None:
        return to_rfc3339(parse_dt(now))
    env = os.environ.get("SCHEDULE_NOW")
    if env:
        return to_rfc3339(parse_dt(env))
    return to_rfc3339(now_utc())


# =================================================================================================
# UUIDv7 (time-ordered) — Python 3.13 has no uuid.uuid7(); implement RFC 9562.
# =================================================================================================
def uuid7():
    ms = int(time.time() * 1000)
    rand = _uuid.uuid4().int
    # 48-bit ms | ver(4)=7 | rand_a(12) | variant(2)=10 | rand_b(62)
    rand_a = rand & 0x0FFF
    rand_b = (rand >> 12) & ((1 << 62) - 1)
    val = (ms & ((1 << 48) - 1)) << 80
    val |= 0x7 << 76
    val |= rand_a << 64
    val |= 0b10 << 62
    val |= rand_b
    return str(_uuid.UUID(int=val))


# =================================================================================================
# DB path / connection / pragmas
# =================================================================================================
def default_db_path():
    return os.environ.get(
        "SCHEDULE_DB_PATH",
        os.path.join(os.path.expanduser("~"), ".claude", "schedule-reminder", "db.sqlite3"),
    )


def _connect(db_path=None):
    """Open a connection in autocommit mode (isolation_level=None) so WE control BEGIN IMMEDIATE."""
    path = db_path or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # Connection-level pragmas (busy_timeout is NOT persistent — must be set per connection).
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _busy_retry(fn):
    """Bounded exponential back-off around transient SQLITE_BUSY/locked."""
    last = None
    for attempt in range(_BUSY_MAX_RETRIES):
        try:
            return fn()
        except sqlite3.OperationalError as e:  # type: ignore
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                last = e
                time.sleep(_BUSY_BASE * (2 ** attempt))
                continue
            raise
    raise SkillError("ERR_BUSY", "database busy after retries: %s" % last)


class _Tx:
    """BEGIN IMMEDIATE short write transaction with commit/rollback + in-process write lock."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        _WRITE_LOCK.acquire()
        _busy_retry(lambda: self.conn.execute("BEGIN IMMEDIATE"))
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        finally:
            _WRITE_LOCK.release()
        return False


# =================================================================================================
# Schema / migrations (additive only)
# =================================================================================================
_DDL = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1,
  kind TEXT NOT NULL DEFAULT 'task',
  title TEXT NOT NULL,
  description TEXT,
  state TEXT NOT NULL DEFAULT 'pending',
  progress INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,
  due_at TEXT, scheduled_at TEXT, start_at TEXT, end_at TEXT, wait_until TEXT,
  tz TEXT, recurrence TEXT, rdate TEXT, exdate TEXT,
  tags TEXT, project TEXT, relations TEXT, alarms TEXT,
  source TEXT,
  idempotency_key TEXT UNIQUE,
  notified_at TEXT, next_retry_at TEXT, retry_count INTEGER NOT NULL DEFAULT 0, claimed_at TEXT,
  block_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  ext TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_state ON items(state);
CREATE INDEX IF NOT EXISTS idx_items_due ON items(due_at);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_state_due ON items(state, due_at);

CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  item_id TEXT,
  actor TEXT,
  event_type TEXT NOT NULL,
  from_state TEXT, to_state TEXT,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_item ON events(item_id);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def init_db(db_path=None):
    """Create schema, enable WAL (durable per-file property), set user_version. Idempotent."""
    path = db_path or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # persists for the file; set once
        conn.executescript(_DDL)
        cur = conn.execute("PRAGMA user_version")
        ver = cur.fetchone()[0]
        if ver < SCHEMA_USER_VERSION:
            # additive migrations would run here in order; v1 is the base schema above.
            conn.execute("PRAGMA user_version = %d" % SCHEMA_USER_VERSION)
        return path
    finally:
        conn.close()


# =================================================================================================
# Row <-> dict
# =================================================================================================
def _row_to_item(row):
    if row is None:
        return None
    d = dict(row)
    for c in _JSON_COLS:
        v = d.get(c)
        if v is None or v == "":
            d[c] = None
        else:
            try:
                d[c] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                d[c] = v
    return d


def _dump_json(v):
    if v is None:
        return None
    if isinstance(v, str):
        # already a JSON string? keep; else store as JSON string
        try:
            json.loads(v)
            return v
        except (json.JSONDecodeError, ValueError):
            return json.dumps(v, ensure_ascii=False)
    return json.dumps(v, ensure_ascii=False)


def _get_raw(conn, item_id):
    cur = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    return cur.fetchone()


def _append_event(conn, item_id, actor, event_type, from_state=None, to_state=None, payload=None):
    conn.execute(
        "INSERT INTO events(ts,item_id,actor,event_type,from_state,to_state,payload) "
        "VALUES(?,?,?,?,?,?,?)",
        (to_rfc3339(now_utc()), item_id, actor, event_type, from_state, to_state,
         json.dumps(payload, ensure_ascii=False) if payload is not None else None),
    )


# =================================================================================================
# Validation / invariants
# =================================================================================================
def _validate_kind(kind):
    if kind not in KINDS:
        raise SkillError("ERR_BAD_KIND", "kind must be one of %s" % (KINDS,), kind=kind)


def _validate_state(state):
    if state not in STATES:
        raise SkillError("ERR_BAD_STATE", "state must be one of %s" % (STATES,), state=state)


def _deps_satisfied(conn, relations):
    """All depends-on targets must be in state done."""
    if not relations:
        return True, []
    unmet = []
    for rel in relations:
        if isinstance(rel, dict) and rel.get("type") == "depends-on":
            tgt = rel.get("target_id")
            row = _get_raw(conn, tgt)
            if row is None or row["state"] != "done":
                unmet.append(tgt)
    return (len(unmet) == 0), unmet


def _enforce_terminal_fields(fields, to_state, now):
    """done/cancelled MUST have end_at; done forces progress=100."""
    if to_state == "done":
        fields.setdefault("end_at", now)
        if fields.get("end_at") is None:
            fields["end_at"] = now
        fields["progress"] = 100
    elif to_state == "cancelled":
        if fields.get("end_at") is None:
            fields["end_at"] = now


# =================================================================================================
# Public API — write ops (each = BEGIN IMMEDIATE short tx)
# =================================================================================================
def add_item(title, *, kind="task", due_at=None, state="pending", priority=0, progress=0,
             description=None, scheduled_at=None, start_at=None, end_at=None, wait_until=None,
             tz=None, recurrence=None, rdate=None, exdate=None, tags=None, project=None,
             relations=None, alarms=None, source=None, idempotency_key=None, ext=None,
             actor=None, db_path=None, _id=None):
    """Create an item. Idempotent on idempotency_key (UPSERT). Returns the item dict."""
    if not title or not str(title).strip():
        raise SkillError("ERR_BAD_INPUT", "title is required")
    _validate_kind(kind)
    _validate_state(state)
    now = to_rfc3339(now_utc())
    item_id = _id or uuid7()

    # normalise time fields to canonical RFC3339
    def t(x):
        return to_rfc3339(parse_dt(x)) if x else None

    fields = {
        "id": item_id, "schema_version": RECORD_SCHEMA_VERSION, "kind": kind,
        "title": str(title), "description": description, "state": state,
        "progress": int(progress), "priority": int(priority),
        "due_at": t(due_at), "scheduled_at": t(scheduled_at), "start_at": t(start_at),
        "end_at": t(end_at), "wait_until": t(wait_until), "tz": tz, "recurrence": recurrence,
        "rdate": _dump_json(rdate), "exdate": _dump_json(exdate), "tags": _dump_json(tags),
        "project": project, "relations": _dump_json(relations), "alarms": _dump_json(alarms),
        "source": source, "idempotency_key": idempotency_key,
        "notified_at": None, "next_retry_at": None, "retry_count": 0, "claimed_at": None,
        "block_reason": None, "created_at": now, "updated_at": now, "ext": _dump_json(ext),
    }
    _enforce_terminal_fields(fields, state, now)

    conn = _connect(db_path)
    try:
        with _Tx(conn):
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM items WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    # idempotent replay: merge ext, refresh mutable fields, keep original id
                    merged_ext = _merge_ext(existing["ext"], ext)
                    conn.execute(
                        "UPDATE items SET title=?, description=?, due_at=?, priority=?, "
                        "tags=?, source=?, ext=?, updated_at=? WHERE idempotency_key=?",
                        (str(title), description, fields["due_at"], int(priority),
                         _dump_json(tags), source, merged_ext, now, idempotency_key),
                    )
                    _append_event(conn, existing["id"], actor or source, "idempotent_replay")
                    return _row_to_item(_get_raw(conn, existing["id"]))
            cols = ", ".join(ITEM_COLUMNS)
            ph = ", ".join("?" for _ in ITEM_COLUMNS)
            conn.execute("INSERT INTO items(%s) VALUES(%s)" % (cols, ph),
                         tuple(fields[c] for c in ITEM_COLUMNS))
            _append_event(conn, item_id, actor or source, "created", to_state=state)
            return _row_to_item(_get_raw(conn, item_id))
    finally:
        conn.close()


def _merge_ext(existing_json, new_ext):
    """Merge ext dicts, MUST-PRESERVE existing unknown keys."""
    base = {}
    if existing_json:
        try:
            base = json.loads(existing_json) or {}
        except (json.JSONDecodeError, TypeError):
            base = {}
    if new_ext:
        if isinstance(new_ext, str):
            try:
                new_ext = json.loads(new_ext)
            except (json.JSONDecodeError, ValueError):
                new_ext = {}
        if isinstance(new_ext, dict):
            base.update(new_ext)
    return json.dumps(base, ensure_ascii=False) if base else None


# fields a generic update may set (state changes MUST go through transition())
_UPDATABLE = {
    "title", "description", "kind", "priority", "progress", "due_at", "scheduled_at",
    "start_at", "end_at", "wait_until", "tz", "recurrence", "rdate", "exdate", "tags",
    "project", "relations", "alarms", "source", "block_reason",
}
_TIME_FIELDS = {"due_at", "scheduled_at", "start_at", "end_at", "wait_until"}


def update_item(item_id, *, idempotency_key=None, actor=None, ext=None, db_path=None, **fields):
    """Patch mutable fields. ext is deep-merged (unknown keys preserved). State is NOT changed here."""
    conn = _connect(db_path)
    try:
        with _Tx(conn):
            row = _get_raw(conn, item_id)
            if row is None:
                raise SkillError("ERR_NOT_FOUND", "no item with id %s" % item_id, id=item_id)
            if "state" in fields:
                raise SkillError("ERR_USE_TRANSITION",
                                 "state changes must go through transition()/done/block",
                                 id=item_id)
            sets, vals = [], []
            for k, v in fields.items():
                if k not in _UPDATABLE:
                    raise SkillError("ERR_BAD_FIELD", "field not updatable: %s" % k, field=k)
                if k in _TIME_FIELDS:
                    v = to_rfc3339(parse_dt(v)) if v else None
                elif k in _JSON_COLS:
                    v = _dump_json(v)
                sets.append("%s=?" % k)
                vals.append(v)
            merged_ext = _merge_ext(row["ext"], ext)
            sets.append("ext=?"); vals.append(merged_ext)
            now = to_rfc3339(now_utc())
            sets.append("updated_at=?"); vals.append(now)
            vals.append(item_id)
            conn.execute("UPDATE items SET %s WHERE id=?" % ", ".join(sets), tuple(vals))
            _append_event(conn, item_id, actor, "updated",
                          payload={"fields": sorted(fields.keys())})
            return _row_to_item(_get_raw(conn, item_id))
    finally:
        conn.close()


def transition(item_id, to_state, *, expect_state=None, reason=None, actor=None,
               progress=None, db_path=None):
    """Move item to to_state with state-machine + invariant enforcement and optimistic CAS."""
    _validate_state(to_state)
    conn = _connect(db_path)
    try:
        with _Tx(conn):
            row = _get_raw(conn, item_id)
            if row is None:
                raise SkillError("ERR_NOT_FOUND", "no item with id %s" % item_id, id=item_id)
            cur_state = row["state"]
            if expect_state is not None and cur_state != expect_state:
                raise SkillError("ERR_STATE_CONFLICT",
                                 "expected state %s but found %s" % (expect_state, cur_state),
                                 id=item_id, current=cur_state, expected=expect_state)
            if to_state == cur_state:
                # no-op transition is allowed and idempotent
                return _row_to_item(row)
            allowed = TRANSITIONS.get(cur_state, set())
            if to_state not in allowed:
                raise SkillError("ERR_ILLEGAL_TRANSITION",
                                 "cannot move %s -> %s" % (cur_state, to_state),
                                 id=item_id, current=cur_state, to=to_state,
                                 allowed=sorted(allowed))
            now = to_rfc3339(now_utc())
            relations = json.loads(row["relations"]) if row["relations"] else None

            new_fields = {"state": to_state, "updated_at": now}
            if progress is not None:
                new_fields["progress"] = int(progress)

            if to_state == "done":
                ok, unmet = _deps_satisfied(conn, relations)
                if not ok:
                    raise SkillError("ERR_DEPENDENCY_UNMET",
                                     "depends-on not done: %s" % unmet, id=item_id, unmet=unmet)
                new_fields["end_at"] = now
                new_fields["progress"] = 100
            elif to_state == "cancelled":
                new_fields["end_at"] = now
            elif to_state == "blocked":
                _, unmet = _deps_satisfied(conn, relations)
                if not unmet and not reason and not row["block_reason"]:
                    raise SkillError("ERR_BLOCK_REASON_REQUIRED",
                                     "blocked requires an unmet blocker or a reason", id=item_id)
                if reason:
                    new_fields["block_reason"] = reason
            elif to_state == "pending":
                # reopen from terminal: clear end_at + delivery bookkeeping
                if cur_state in TERMINAL_STATES:
                    new_fields["end_at"] = None
                    new_fields["claimed_at"] = None

            sets = ", ".join("%s=?" % k for k in new_fields)
            vals = list(new_fields.values()) + [item_id, cur_state]
            cur = conn.execute(
                "UPDATE items SET %s WHERE id=? AND state=?" % sets, tuple(vals)
            )
            if cur.rowcount == 0:  # CAS lost: state changed under us
                fresh = _get_raw(conn, item_id)
                raise SkillError("ERR_STATE_CONFLICT", "state changed concurrently",
                                 id=item_id, current=fresh["state"] if fresh else None)
            _append_event(conn, item_id, actor, "status_change",
                          from_state=cur_state, to_state=to_state,
                          payload={"reason": reason} if reason else None)
            return _row_to_item(_get_raw(conn, item_id))
    finally:
        conn.close()


def done(item_id, *, actor=None, db_path=None):
    return transition(item_id, "done", actor=actor, db_path=db_path)


def block(item_id, *, blocker_id=None, reason=None, actor=None, db_path=None):
    """Block an item; optionally record a depends-on blocker relation first."""
    if blocker_id:
        cur = get_item(item_id, db_path=db_path)
        rels = cur.get("relations") or [] if cur else []
        rels.append({"type": "depends-on", "target_id": blocker_id})
        update_item(item_id, relations=rels, actor=actor, db_path=db_path)
    return transition(item_id, "blocked", reason=reason, actor=actor, db_path=db_path)


def snooze(item_id, until, *, actor=None, db_path=None):
    """Suppress reminders until `until` and clear any pending notify state. Does not change state."""
    until_s = to_rfc3339(parse_dt(until))
    conn = _connect(db_path)
    try:
        with _Tx(conn):
            row = _get_raw(conn, item_id)
            if row is None:
                raise SkillError("ERR_NOT_FOUND", "no item with id %s" % item_id, id=item_id)
            now = to_rfc3339(now_utc())
            conn.execute(
                "UPDATE items SET wait_until=?, notified_at=NULL, next_retry_at=NULL, "
                "claimed_at=NULL, updated_at=? WHERE id=?",
                (until_s, now, item_id),
            )
            _append_event(conn, item_id, actor, "snoozed", payload={"until": until_s})
            return _row_to_item(_get_raw(conn, item_id))
    finally:
        conn.close()


# =================================================================================================
# Public API — read ops (no transaction; WAL gives consistent snapshot reads)
# =================================================================================================
def get_item(item_id, *, db_path=None):
    conn = _connect(db_path)
    try:
        return _row_to_item(_get_raw(conn, item_id))
    finally:
        conn.close()


def list_items(*, state=None, source=None, kind=None, due_before=None, active_only=False,
               limit=100, cursor=None, db_path=None):
    """List items with simple filters + keyset pagination by id (UUIDv7 is time-ordered)."""
    conn = _connect(db_path)
    try:
        where, params = [], []
        if state:
            where.append("state = ?"); params.append(state)
        if source:
            where.append("source = ?"); params.append(source)
        if kind:
            where.append("kind = ?"); params.append(kind)
        if active_only:
            where.append("state IN (%s)" % ",".join("?" * len(ACTIVE_STATES)))
            params.extend(ACTIVE_STATES)
        if due_before:
            where.append("due_at IS NOT NULL AND due_at <= ?")
            params.append(to_rfc3339(parse_dt(due_before)))
        if cursor:
            where.append("id > ?"); params.append(cursor)
        sql = "SELECT * FROM items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(int(limit) + 1)
        rows = conn.execute(sql, tuple(params)).fetchall()
        items = [_row_to_item(r) for r in rows[:limit]]
        next_cursor = items[-1]["id"] if len(rows) > limit and items else None
        return {"items": items, "next_cursor": next_cursor}
    finally:
        conn.close()


def due_items(*, now=None, lead=0, db_path=None):
    """Read-only: items whose due_at - lead <= now and still active and not waiting."""
    now_s = resolve_now(now)
    conn = _connect(db_path)
    try:
        # lead applied by comparing due_at <= now + lead, i.e. due_at <= threshold
        threshold = now_s
        if lead:
            from datetime import timedelta
            threshold = to_rfc3339(parse_dt(now_s) + timedelta(seconds=int(lead)))
        rows = conn.execute(
            "SELECT * FROM items WHERE due_at IS NOT NULL AND due_at <= ? "
            "AND state NOT IN ('done','cancelled') "
            "AND (wait_until IS NULL OR wait_until <= ?) ORDER BY due_at ASC",
            (threshold, now_s),
        ).fetchall()
        return [_row_to_item(r) for r in rows]
    finally:
        conn.close()


# =================================================================================================
# tick — due dispatch reconciliation (at-least-once + idempotent dedupe + back-off retry)
# =================================================================================================
def _default_notify(item):
    """Bridge to notify.notify; imported lazily to keep store decoupled."""
    try:
        from notify import notify  # type: ignore
    except Exception:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location("notify", os.path.join(here, "notify.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        notify = mod.notify
    title = item.get("title", "(no title)")
    due = item.get("due_at", "")
    text = "[reminder] %s%s" % (title, ("  (due %s)" % due if due else ""))
    return notify(text)


def tick(*, now=None, lead=0, dry_run=False, notify_fn=None, db_path=None, actor="tick"):
    """Reconcile due items: claim -> notify (outside tx) -> mark notified | back-off retry."""
    now_s = resolve_now(now)
    notify_fn = notify_fn or _default_notify
    conn = _connect(db_path)
    dispatched, retried, blocked, skipped = [], [], [], []
    try:
        from datetime import timedelta
        threshold = to_rfc3339(parse_dt(now_s) + timedelta(seconds=int(lead))) if lead else now_s
        rows = conn.execute(
            "SELECT * FROM items WHERE due_at IS NOT NULL AND due_at <= ? "
            "AND state NOT IN ('done','cancelled') "
            "AND notified_at IS NULL "
            "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
            "AND (wait_until IS NULL OR wait_until <= ?) ORDER BY due_at ASC",
            (threshold, now_s, now_s),
        ).fetchall()

        for row in rows:
            item_id = row["id"]
            # atomic claim: only one tick may grab a not-yet-notified item
            with _Tx(conn):
                cur = conn.execute(
                    "UPDATE items SET claimed_at=? WHERE id=? AND notified_at IS NULL",
                    (now_s, item_id),
                )
                claimed = cur.rowcount > 0
            if not claimed:
                skipped.append(item_id)
                continue

            if dry_run:
                dispatched.append(item_id)
                continue

            item = _row_to_item(row)
            try:
                ok = bool(notify_fn(item))
            except Exception as e:  # notify must never crash the tick loop
                ok = False
                _record_notify_failure(conn, item_id, now_s, str(e), blocked, retried, actor)
                continue

            if ok:
                with _Tx(conn):
                    conn.execute(
                        "UPDATE items SET notified_at=?, next_retry_at=NULL, updated_at=? "
                        "WHERE id=? AND notified_at IS NULL",
                        (now_s, now_s, item_id),
                    )
                    _append_event(conn, item_id, actor, "notified",
                                  payload={"channel": "discord"})
                dispatched.append(item_id)
            else:
                _record_notify_failure(conn, item_id, now_s, "notify returned falsey",
                                       blocked, retried, actor)

        # watchdog / self-monitor
        with _Tx(conn):
            conn.execute("INSERT INTO meta(key,value) VALUES('last_tick_at',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now_s,))
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('tick_count','1') "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)")
        return {"dispatched": dispatched, "retried": retried, "blocked": blocked,
                "skipped": skipped, "now": now_s}
    finally:
        conn.close()


def _record_notify_failure(conn, item_id, now_s, detail, blocked, retried, actor):
    from datetime import timedelta
    with _Tx(conn):
        row = _get_raw(conn, item_id)
        rc = (row["retry_count"] or 0) + 1
        if rc >= _NOTIFY_MAX_RETRIES:
            conn.execute(
                "UPDATE items SET retry_count=?, state='blocked', "
                "block_reason='notify channel failed', claimed_at=NULL, updated_at=? WHERE id=?",
                (rc, now_s, item_id),
            )
            _append_event(conn, item_id, actor, "notify_failed_blocked",
                          from_state=row["state"], to_state="blocked",
                          payload={"retry_count": rc, "detail": detail})
            blocked.append(item_id)
        else:
            backoff = min(_NOTIFY_BACKOFF_BASE * (2 ** (rc - 1)), _NOTIFY_BACKOFF_CAP)
            nxt = to_rfc3339(parse_dt(now_s) + timedelta(seconds=backoff))
            conn.execute(
                "UPDATE items SET retry_count=?, next_retry_at=?, claimed_at=NULL, updated_at=? "
                "WHERE id=?",
                (rc, nxt, now_s, item_id),
            )
            _append_event(conn, item_id, actor, "notify_failed",
                          payload={"retry_count": rc, "next_retry_at": nxt, "detail": detail})
            retried.append(item_id)


# =================================================================================================
# events / health
# =================================================================================================
def get_events(item_id, *, limit=200, db_path=None):
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE item_id=? ORDER BY seq ASC LIMIT ?",
            (item_id, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _ver_tuple(s):
    return tuple(int(x) for x in s.split("."))


def health(*, db_path=None, check_task=False):
    path = db_path or default_db_path()
    out = {
        "api_version": API_VERSION,
        "schema_user_version": None,
        "sqlite_backend": _SQLITE_BACKEND,
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_version_ok": _ver_tuple(sqlite3.sqlite_version) >= _ver_tuple(RECOMMENDED_SQLITE),
        "recommended_sqlite": RECOMMENDED_SQLITE,
        "db_path": path,
        "db_ok": False, "wal_ok": False, "integrity_ok": False,
        "relay_ok": False, "task_ok": None,
        "warnings": [],
    }
    try:
        conn = _connect(path)
        try:
            out["db_ok"] = True
            out["schema_user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
            jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
            out["wal_ok"] = (str(jm).lower() == "wal")
            ic = conn.execute("PRAGMA quick_check").fetchone()[0]
            out["integrity_ok"] = (str(ic).lower() == "ok")
        finally:
            conn.close()
    except Exception as e:
        out["warnings"].append("db error: %s" % e)

    if not out["sqlite_version_ok"]:
        out["warnings"].append(
            "SQLite %s < recommended %s: WAL-reset multi-writer corruption bug present; "
            "install pysqlite3-binary or a newer Python for full safety."
            % (sqlite3.sqlite_version, RECOMMENDED_SQLITE))

    # relay reachability = files present, no token echo
    relay = os.environ.get("SCHEDULE_RELAY_SEND",
                           os.path.join(os.path.expanduser("~"), ".claude", "discord_relay",
                                        "send.py"))
    relay_cmd = os.environ.get("SCHEDULE_RELAY_CMD")
    out["relay_ok"] = bool(relay_cmd) or os.path.isfile(relay)
    if not out["relay_ok"]:
        out["warnings"].append("relay not found: set SCHEDULE_RELAY_CMD or install discord_relay")

    if check_task:
        try:
            import subprocess
            r = subprocess.run(["schtasks", "/Query", "/TN", "ScheduleReminderTick"],
                               capture_output=True, text=True)
            out["task_ok"] = (r.returncode == 0)
        except Exception:
            out["task_ok"] = False
    return out
