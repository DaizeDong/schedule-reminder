#!/usr/bin/env python3
"""schedule-reminder — the stable external contract (CLI + JSON).

This is the ONLY surface downstream skills (email-monitor, daily-hotspots, demand-mining,
promotion-assistant) may depend on. They call it via subprocess and parse stdout JSON. They MUST
NOT read the .db file, build SQL, or import internal tables. See ../reference/contract.md.

Conventions (frozen — additive evolution only; deletes/renames bump api_version):
  * stdout: one JSON object (JSON Lines) on success, with top-level api_version + schema_version.
  * stderr: one JSON object {api_version, error_code, message, ...} on failure.
  * exit code: 0 success, 1 structured error, 2 usage error.
  * time: UTC RFC3339 everywhere; inject a clock with --now or SCHEDULE_NOW (tests).
  * db: --db PATH or SCHEDULE_DB_PATH (test isolation).
  * idempotency: add/update accept --idempotency-key (UPSERT); reads are naturally idempotent.

Usage:
  reminder.py init
  reminder.py add --title T [--kind task|event] [--due-at ISO] [--state S] [--priority N]
                  [--tags a,b] [--source S] [--idempotency-key K] [--description D] [--ext JSON]
  reminder.py get --id ID
  reminder.py list [--state S] [--source S] [--kind K] [--due-before ISO] [--active]
                   [--limit N] [--cursor C]
  reminder.py query ...                 (alias of list)
  reminder.py update --id ID [field=...] [--ext JSON] [--idempotency-key K]
  reminder.py transition --id ID --to STATE [--expect STATE] [--reason R] [--progress N]
  reminder.py done --id ID
  reminder.py block --id ID [--blocker-id ID] [--reason R]
  reminder.py snooze --id ID --until ISO
  reminder.py due [--now ISO] [--lead SECONDS]
  reminder.py tick [--now ISO] [--lead SECONDS] [--dry-run]
  reminder.py events --id ID
  reminder.py health [--check-task]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Force UTF-8 JSON I/O regardless of the host console code page (Windows defaults to gbk/cp936),
# so downstream skills can always decode stdout/stderr as UTF-8. Contract-critical for non-ASCII.
try:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", newline="\n")
except Exception:
    pass

# allow `import store` / `import notify` regardless of CWD
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402


def _emit(payload):
    out = {"api_version": store.API_VERSION,
           "schema_version": store.RECORD_SCHEMA_VERSION,
           "ok": True}
    out.update(payload)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
    return 0


def _fail(err):
    body = {"api_version": store.API_VERSION, "ok": False}
    if isinstance(err, store.SkillError):
        body.update(err.to_dict())
    else:
        # Unexpected error: surface only the exception *type*, not str(err), which can embed the db
        # path or other host details. Structured SkillErrors above carry their own safe messages.
        body.update({"error_code": "ERR_INTERNAL",
                     "message": "internal error (%s)" % type(err).__name__})
    sys.stderr.write(json.dumps(body, ensure_ascii=False, default=str) + "\n")
    return 1


def _parse_ext(s):
    if not s:
        return None
    try:
        v = json.loads(s)
    except json.JSONDecodeError as e:
        raise store.SkillError("ERR_BAD_JSON", "--ext is not valid JSON: %s" % e)
    if not isinstance(v, dict):
        raise store.SkillError("ERR_BAD_JSON", "--ext must be a JSON object")
    return v


def _tags(s):
    if s is None:
        return None
    return [t.strip() for t in s.split(",") if t.strip()]


def _json_arg(s, flag):
    """Parse a JSON CLI value (used by --alarms/--rdate/--exdate). None passes through."""
    if s is None:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise store.SkillError("ERR_BAD_JSON", "%s is not valid JSON: %s" % (flag, e))


def cmd_init(a):
    path = store.init_db(a.db)
    return _emit({"db_path": path, "schema_user_version": store.SCHEMA_USER_VERSION})


def cmd_add(a):
    item = store.add_item(
        a.title, kind=a.kind, due_at=a.due_at, state=a.state,
        priority=a.priority, progress=a.progress, description=a.description,
        scheduled_at=a.scheduled_at, wait_until=a.wait_until,
        recurrence=a.recurrence, rdate=_json_arg(a.rdate, "--rdate"),
        exdate=_json_arg(a.exdate, "--exdate"), alarms=_json_arg(a.alarms, "--alarms"),
        tags=_tags(a.tags), project=a.project, source=a.source,
        idempotency_key=a.idempotency_key, ext=_parse_ext(a.ext),
        actor=a.actor, db_path=a.db,
    )
    return _emit({"item": item})


def cmd_get(a):
    item = store.get_item(a.id, db_path=a.db)
    if item is None:
        raise store.SkillError("ERR_NOT_FOUND", "no item with id %s" % a.id, id=a.id)
    return _emit({"item": item})


def cmd_list(a):
    res = store.list_items(state=a.state, source=a.source, kind=a.kind,
                           due_before=a.due_before, active_only=a.active,
                           limit=a.limit, cursor=a.cursor, db_path=a.db)
    return _emit(res)


def cmd_update(a):
    fields = {}
    for kv in (a.set or []):
        if "=" not in kv:
            raise store.SkillError("ERR_BAD_INPUT", "set must be field=value: %r" % kv)
        k, v = kv.split("=", 1)
        fields[k.strip()] = v
    item = store.update_item(a.id, idempotency_key=a.idempotency_key, ext=_parse_ext(a.ext),
                             actor=a.actor, db_path=a.db, **fields)
    return _emit({"item": item})


def cmd_transition(a):
    item = store.transition(a.id, a.to, expect_state=a.expect, reason=a.reason,
                            progress=a.progress, actor=a.actor, db_path=a.db)
    return _emit({"item": item})


def cmd_done(a):
    return _emit({"item": store.done(a.id, actor=a.actor, db_path=a.db)})


def cmd_block(a):
    item = store.block(a.id, blocker_id=a.blocker_id, reason=a.reason,
                       actor=a.actor, db_path=a.db)
    return _emit({"item": item})


def cmd_snooze(a):
    return _emit({"item": store.snooze(a.id, a.until, actor=a.actor, db_path=a.db)})


def cmd_due(a):
    items = store.due_items(now=a.now, lead=a.lead, db_path=a.db)
    return _emit({"items": items, "now": store.resolve_now(a.now)})


def cmd_tick(a):
    res = store.tick(now=a.now, lead=a.lead, dry_run=a.dry_run, db_path=a.db)
    return _emit(res)


def cmd_events(a):
    return _emit({"events": store.get_events(a.id, db_path=a.db)})


def cmd_health(a):
    return _emit({"health": store.health(db_path=a.db, check_task=a.check_task)})


def build_parser():
    p = argparse.ArgumentParser(prog="reminder.py", description="schedule-reminder CLI contract")
    p.add_argument("--db", default=None, help="DB path (or SCHEDULE_DB_PATH env)")
    p.add_argument("--actor", default=None, help="who is acting (skill name / user)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)

    s = sub.add_parser("add"); s.set_defaults(fn=cmd_add)
    s.add_argument("--title", required=True)
    s.add_argument("--kind", default="task", choices=list(store.KINDS))
    s.add_argument("--due-at", dest="due_at", default=None)
    s.add_argument("--scheduled-at", dest="scheduled_at", default=None)
    s.add_argument("--wait-until", dest="wait_until", default=None)
    s.add_argument("--state", default="pending", choices=list(store.STATES))
    s.add_argument("--priority", type=int, default=0)
    s.add_argument("--progress", type=int, default=0)
    s.add_argument("--recurrence", default=None, help="RRULE string, e.g. FREQ=DAILY;INTERVAL=1")
    s.add_argument("--rdate", default=None, help="JSON array of extra RFC3339 dates")
    s.add_argument("--exdate", default=None, help="JSON array of excluded RFC3339 dates")
    s.add_argument("--alarms", default=None,
                   help='JSON array of alarm rules, e.g. [{"lead":3600}] or [{"trigger":"-PT15M"}]')
    s.add_argument("--description", default=None)
    s.add_argument("--tags", default=None, help="comma-separated")
    s.add_argument("--project", default=None)
    s.add_argument("--source", default=None)
    s.add_argument("--idempotency-key", dest="idempotency_key", default=None)
    s.add_argument("--ext", default=None, help="JSON object of extra/unknown fields (preserved)")

    s = sub.add_parser("get"); s.set_defaults(fn=cmd_get); s.add_argument("--id", required=True)

    for nm in ("list", "query"):
        s = sub.add_parser(nm); s.set_defaults(fn=cmd_list)
        s.add_argument("--state", default=None)
        s.add_argument("--source", default=None)
        s.add_argument("--kind", default=None)
        s.add_argument("--due-before", dest="due_before", default=None)
        s.add_argument("--active", action="store_true")
        s.add_argument("--limit", type=int, default=100)
        s.add_argument("--cursor", default=None)

    s = sub.add_parser("update"); s.set_defaults(fn=cmd_update)
    s.add_argument("--id", required=True)
    s.add_argument("--set", action="append", help="field=value (repeatable)")
    s.add_argument("--ext", default=None)
    s.add_argument("--idempotency-key", dest="idempotency_key", default=None)

    s = sub.add_parser("transition"); s.set_defaults(fn=cmd_transition)
    s.add_argument("--id", required=True)
    s.add_argument("--to", required=True, choices=list(store.STATES))
    s.add_argument("--expect", default=None, choices=list(store.STATES))
    s.add_argument("--reason", default=None)
    s.add_argument("--progress", type=int, default=None)

    s = sub.add_parser("done"); s.set_defaults(fn=cmd_done); s.add_argument("--id", required=True)

    s = sub.add_parser("block"); s.set_defaults(fn=cmd_block)
    s.add_argument("--id", required=True)
    s.add_argument("--blocker-id", dest="blocker_id", default=None)
    s.add_argument("--reason", default=None)

    s = sub.add_parser("snooze"); s.set_defaults(fn=cmd_snooze)
    s.add_argument("--id", required=True)
    s.add_argument("--until", required=True)

    s = sub.add_parser("due"); s.set_defaults(fn=cmd_due)
    s.add_argument("--now", default=None)
    s.add_argument("--lead", type=int, default=0)

    s = sub.add_parser("tick"); s.set_defaults(fn=cmd_tick)
    s.add_argument("--now", default=None)
    s.add_argument("--lead", type=int, default=0)
    s.add_argument("--dry-run", dest="dry_run", action="store_true")

    s = sub.add_parser("events"); s.set_defaults(fn=cmd_events); s.add_argument("--id", required=True)

    s = sub.add_parser("health"); s.set_defaults(fn=cmd_health)
    s.add_argument("--check-task", dest="check_task", action="store_true")

    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    # ensure DB exists for all but init (init creates it explicitly)
    try:
        if a.cmd != "init":
            store.init_db(a.db)
        return a.fn(a)
    except store.SkillError as e:
        return _fail(e)
    except BrokenPipeError:
        return 0
    except Exception as e:  # last-resort structured error, never a raw traceback to downstream
        return _fail(e)


if __name__ == "__main__":
    sys.exit(main())
