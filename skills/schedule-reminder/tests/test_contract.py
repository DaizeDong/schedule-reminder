#!/usr/bin/env python3
"""schedule-reminder acceptance evals E1-E15 (eval matrix: docs/design-brief.md proof bar; the full
ARCHITECTURE §6 eval matrix lives in the CodesResearch build notes).

Every test drives the FROZEN CLI via subprocess and asserts JSON output (contract, not internals).
Each test gets an isolated DB via SCHEDULE_DB_PATH; the clock is injected with --now; the relay is
replaced by a stub via SCHEDULE_RELAY_CMD so no real Discord push happens.

Red-line gates (block merge if red): E8 (concurrent no-corruption), E9 (concurrent read/write),
E11 (API contract golden), E12 (unknown-field preservation).
"""
import json
import os
import subprocess
import sys
import textwrap
import sqlite3
import concurrent.futures as cf

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
REMINDER = os.path.join(SCRIPTS, "reminder.py")


def run(args, db, env_extra=None, check=True):
    env = dict(os.environ)
    env["SCHEDULE_DB_PATH"] = db
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    r = subprocess.run([sys.executable, REMINDER] + args,
                       capture_output=True, text=True, encoding="utf-8", env=env)
    if check and r.returncode != 0:
        raise AssertionError("cmd failed rc=%s args=%s\nstdout=%s\nstderr=%s"
                             % (r.returncode, args, r.stdout, r.stderr))
    return r


def jout(r):
    return json.loads(r.stdout.strip().splitlines()[-1])


def jerr(r):
    return json.loads(r.stderr.strip().splitlines()[-1])


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "t.sqlite3")
    run(["init"], p)
    return p


def make_stub(tmp_path, fail=False):
    """A relay stub script: records each call to <log>, returns 0 (ok) or 1 (fail)."""
    log = str(tmp_path / "relay.log")
    stub = str(tmp_path / "stub.py")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent("""\
            import sys
            log = %r
            with open(log, 'a', encoding='utf-8') as fh:
                fh.write((sys.argv[1] if len(sys.argv) > 1 else '') + '\\n')
            sys.exit(%d)
        """) % (log, 1 if fail else 0))
    cmd = '%s %s' % (sys.executable, stub)
    return cmd, log


# ----------------------------------------------------------------------------- E1 CRUD correctness
def test_e1_crud(db):
    r = run(["add", "--title", "Task A", "--priority", "1", "--tags", "x,y"], db)
    item = jout(r)["item"]
    iid = item["id"]
    assert item["state"] == "pending" and item["progress"] == 0 and item["tags"] == ["x", "y"]
    assert jout(r)["api_version"] and jout(r)["schema_version"] == 1

    got = jout(run(["get", "--id", iid], db))["item"]
    assert got["id"] == iid and got["title"] == "Task A"

    lst = jout(run(["list"], db))
    assert any(i["id"] == iid for i in lst["items"])

    upd = jout(run(["update", "--id", iid, "--set", "title=Task A2",
                    "--set", "priority=3"], db))["item"]
    assert upd["title"] == "Task A2" and upd["priority"] == 3

    fin = jout(run(["done", "--id", iid], db))["item"]
    assert fin["state"] == "done" and fin["end_at"] and fin["progress"] == 100


# ----------------------------------------------------------- E2 state machine (table-driven, full)
LEGAL = [
    ("pending", "doing"), ("pending", "blocked"), ("pending", "done"), ("pending", "cancelled"),
    ("doing", "done"), ("doing", "blocked"), ("doing", "pending"), ("doing", "cancelled"),
    ("blocked", "doing"), ("blocked", "pending"), ("blocked", "done"), ("blocked", "cancelled"),
    ("done", "pending"), ("cancelled", "pending"),
]
ALL_PAIRS = [(a, b) for a in ("pending", "doing", "done", "blocked", "cancelled")
             for b in ("pending", "doing", "done", "blocked", "cancelled") if a != b]
ILLEGAL = [p for p in ALL_PAIRS if p not in LEGAL]


def _drive_to(db, target):
    """Create an item and move it into `target` via a known-legal path."""
    iid = jout(run(["add", "--title", "sm"], db))["item"]["id"]
    if target == "pending":
        return iid
    if target == "doing":
        run(["transition", "--id", iid, "--to", "doing"], db)
    elif target == "blocked":
        run(["transition", "--id", iid, "--to", "blocked", "--reason", "r"], db)
    elif target == "done":
        run(["transition", "--id", iid, "--to", "done"], db)
    elif target == "cancelled":
        run(["transition", "--id", iid, "--to", "cancelled"], db)
    return iid


@pytest.mark.parametrize("frm,to", LEGAL)
def test_e2_legal_transitions(db, frm, to):
    iid = _drive_to(db, frm)
    args = ["transition", "--id", iid, "--to", to]
    if to == "blocked":
        args += ["--reason", "r"]
    r = run(args, db)
    item = jout(r)["item"]
    assert item["state"] == to


@pytest.mark.parametrize("frm,to", ILLEGAL)
def test_e2_illegal_transitions(db, frm, to):
    iid = _drive_to(db, frm)
    args = ["transition", "--id", iid, "--to", to]
    if to == "blocked":
        args += ["--reason", "r"]
    r = run(args, db, check=False)
    assert r.returncode != 0
    err = jerr(r)
    assert err["error_code"] == "ERR_ILLEGAL_TRANSITION"
    assert err["current"] == frm and "allowed" in err


def test_e2_terminal_write_protection(db):
    # done may only reopen to pending; doing/blocked/cancelled are rejected
    iid = _drive_to(db, "done")
    for bad in ("doing", "blocked", "cancelled"):
        r = run(["transition", "--id", iid, "--to", bad, "--reason", "r"], db, check=False)
        assert r.returncode != 0 and jerr(r)["error_code"] == "ERR_ILLEGAL_TRANSITION"


# --------------------------------------------------------------- E3 state/progress invariants
def test_e3_invariants(db):
    # done forces end_at + progress=100 even if not supplied
    iid = jout(run(["add", "--title", "inv"], db))["item"]["id"]
    item = jout(run(["transition", "--id", iid, "--to", "done"], db))["item"]
    assert item["end_at"] and item["progress"] == 100

    # blocked with neither blocker nor reason is rejected
    iid2 = jout(run(["add", "--title", "inv2"], db))["item"]["id"]
    r = run(["transition", "--id", iid2, "--to", "blocked"], db, check=False)
    assert r.returncode != 0 and jerr(r)["error_code"] == "ERR_BLOCK_REASON_REQUIRED"

    # update cannot change state (must go through transition)
    iid3 = jout(run(["add", "--title", "inv3"], db))["item"]["id"]
    r = run(["update", "--id", iid3, "--set", "state=done"], db, check=False)
    assert r.returncode != 0 and jerr(r)["error_code"] == "ERR_USE_TRANSITION"

    # transition to done with an unmet depends-on is rejected
    dep = jout(run(["add", "--title", "dep"], db))["item"]["id"]
    main = jout(run(["add", "--title", "main"], db))["item"]["id"]
    run(["update", "--id", main, "--set",
         "relations=[{\"type\":\"depends-on\",\"target_id\":\"%s\"}]" % dep], db)
    r = run(["transition", "--id", main, "--to", "done"], db, check=False)
    assert r.returncode != 0 and jerr(r)["error_code"] == "ERR_DEPENDENCY_UNMET"
    run(["done", "--id", dep], db)  # satisfy dep
    assert jout(run(["done", "--id", main], db))["item"]["state"] == "done"


# --------------------------------------------------------------- E4 due trigger (injected clock)
def test_e4_due_selection(db, tmp_path):
    past = jout(run(["add", "--title", "past", "--due-at", "2026-01-01T00:00:00Z"], db))["item"]["id"]
    now_due = jout(run(["add", "--title", "now", "--due-at", "2026-06-25T12:00:00Z"], db))["item"]["id"]
    future = jout(run(["add", "--title", "fut", "--due-at", "2026-12-31T00:00:00Z"], db))["item"]["id"]
    stub, log = make_stub(tmp_path)
    res = jout(run(["tick", "--now", "2026-06-25T12:00:00Z"], db,
                   {"SCHEDULE_RELAY_CMD": stub}))
    disp = set(res["dispatched"])
    assert past in disp and now_due in disp and future not in disp


# --------------------------------------------------------------- E5 due idempotency (double tick)
def test_e5_idempotent_tick(db, tmp_path):
    iid = jout(run(["add", "--title", "x", "--due-at", "2026-01-01T00:00:00Z"], db))["item"]["id"]
    stub, log = make_stub(tmp_path)
    r1 = jout(run(["tick", "--now", "2026-06-25T12:00:00Z"], db, {"SCHEDULE_RELAY_CMD": stub}))
    r2 = jout(run(["tick", "--now", "2026-06-25T12:00:00Z"], db, {"SCHEDULE_RELAY_CMD": stub}))
    assert iid in r1["dispatched"]
    assert r2["dispatched"] == []  # no re-dispatch
    with open(log, encoding="utf-8") as f:
        assert len([ln for ln in f if ln.strip()]) == 1  # delivered exactly once


# --------------------------------------------------------------- E6 missed-fire catch-up
def test_e6_missed_fire(db, tmp_path):
    ids = []
    for i in range(5):
        ids.append(jout(run(["add", "--title", "m%d" % i,
                             "--due-at", "2026-0%d-01T00:00:00Z" % (i + 1)], db))["item"]["id"])
    stub, log = make_stub(tmp_path)
    res = jout(run(["tick", "--now", "2026-06-25T12:00:00Z"], db, {"SCHEDULE_RELAY_CMD": stub}))
    assert set(res["dispatched"]) == set(ids)  # all overdue caught up in one tick


# --------------------------------------------------------------- E7 retry back-off -> blocked
def test_e7_retry_backoff(db, tmp_path):
    iid = jout(run(["add", "--title", "r", "--due-at", "2026-01-01T00:00:00Z"], db))["item"]["id"]
    fail_stub, _ = make_stub(tmp_path, fail=True)
    # 5 failing ticks at advancing clocks -> retry_count climbs, next_retry_at monotonic, then blocked
    prev_retry = None
    for k in range(5):
        nowt = "2026-06-25T12:%02d:00Z" % (k * 10)
        run(["tick", "--now", nowt], db, {"SCHEDULE_RELAY_CMD": fail_stub})
        item = jout(run(["get", "--id", iid], db))["item"]
        assert item["notified_at"] is None  # never marked notified on failure
        if item["state"] == "blocked":
            assert item["block_reason"] == "notify channel failed"
            break
        assert item["next_retry_at"] is not None
        if prev_retry is not None:
            assert item["next_retry_at"] > prev_retry  # monotonic back-off
        prev_retry = item["next_retry_at"]
    else:
        pytest.fail("item never blocked after max retries")
    assert jout(run(["get", "--id", iid], db))["item"]["state"] == "blocked"


# --------------------------------------------------------------- E8 concurrent writes no corruption
def _writer(db, n, tag):
    for i in range(n):
        run(["add", "--title", "%s-%d" % (tag, i), "--source", tag], db)


def test_e8_concurrent_writes(db):
    N, M = 8, 15
    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        futs = [ex.submit(_writer, db, M, "w%d" % w) for w in range(N)]
        for f in futs:
            f.result()
    lst = jout(run(["list", "--limit", "1000"], db))
    assert len(lst["items"]) == N * M  # no lost writes
    assert len(set(i["id"] for i in lst["items"])) == N * M  # all ids unique
    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


# --------------------------------------------------------------- E9 concurrent read/write consistency
def test_e9_concurrent_read_write(db):
    errors = []

    def writer():
        try:
            for i in range(20):
                run(["add", "--title", "rw-%d" % i, "--source", "rw"], db)
        except Exception as e:
            errors.append(("w", str(e)))

    def reader():
        try:
            for _ in range(30):
                r = run(["list", "--limit", "1000"], db)
                for it in jout(r)["items"]:
                    assert "id" in it and "state" in it and "title" in it  # no half-written rows
        except Exception as e:
            errors.append(("r", str(e)))

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(writer), ex.submit(reader), ex.submit(reader), ex.submit(writer)]
        for f in futs:
            f.result()
    assert not errors, errors


# --------------------------------------------------------------- E10 idempotent write dedupe
def test_e10_idempotent_write(db):
    a = jout(run(["add", "--title", "dup", "--idempotency-key", "K1", "--source", "s"], db))["item"]
    b = jout(run(["add", "--title", "dup2", "--idempotency-key", "K1", "--source", "s"], db))["item"]
    assert a["id"] == b["id"]
    lst = jout(run(["list", "--limit", "100"], db))
    assert sum(1 for i in lst["items"] if i["idempotency_key"] == "K1") == 1


# --------------------------------------------------------------- E11 API contract golden
GOLDEN_VERBS = {"init", "add", "get", "list", "query", "update", "transition", "done",
                "block", "snooze", "due", "tick", "events", "health"}
GOLDEN_ITEM_FIELDS = {
    "id", "schema_version", "kind", "title", "description", "state", "progress", "priority",
    "due_at", "scheduled_at", "start_at", "end_at", "wait_until", "tz", "recurrence", "rdate",
    "exdate", "tags", "project", "relations", "alarms", "source", "idempotency_key",
    "notified_at", "next_retry_at", "retry_count", "claimed_at", "block_reason",
    "created_at", "updated_at", "ext",
}
GOLDEN_STATES = {"pending", "doing", "done", "blocked", "cancelled"}


def test_e11_contract_golden(db):
    sys.path.insert(0, SCRIPTS)
    import importlib
    st = importlib.import_module("store")
    # verbs (subparsers) frozen
    r = run(["--help"], db, check=False)
    for v in GOLDEN_VERBS:
        assert v in r.stdout, "verb missing from CLI: %s" % v
    # item field set frozen (additive only -> equality is the red line for this api_version)
    item = jout(run(["add", "--title", "g"], db))["item"]
    assert set(item.keys()) == GOLDEN_ITEM_FIELDS, set(item.keys()) ^ GOLDEN_ITEM_FIELDS
    # state enum frozen
    assert set(st.STATES) == GOLDEN_STATES
    # top-level envelope keys
    out = jout(run(["add", "--title", "g2"], db))
    assert {"api_version", "schema_version", "ok"} <= set(out.keys())


# --------------------------------------------------------------- E12 unknown field preservation
def test_e12_unknown_field_preserved(db):
    iid = jout(run(["add", "--title", "ext", "--ext",
                    '{"x_promotion_campaign_id":"c-42","x_email_uid":99}'], db))["item"]["id"]
    # mutate an unrelated field
    run(["update", "--id", iid, "--set", "title=ext2"], db)
    run(["transition", "--id", iid, "--to", "doing"], db)
    item = jout(run(["get", "--id", iid], db))["item"]
    assert item["ext"]["x_promotion_campaign_id"] == "c-42"
    assert item["ext"]["x_email_uid"] == 99
    assert item["title"] == "ext2"  # the real change applied
    # adding more ext keys merges, never drops existing
    run(["update", "--id", iid, "--ext", '{"x_new":"v"}'], db)
    item2 = jout(run(["get", "--id", iid], db))["item"]
    assert item2["ext"]["x_promotion_campaign_id"] == "c-42" and item2["ext"]["x_new"] == "v"


# --------------------------------------------------------------- E13 health self-check
def test_e13_health(db):
    h = jout(run(["health"], db))["health"]
    assert h["db_ok"] and h["wal_ok"] and h["integrity_ok"]
    assert "sqlite_version" in h and "sqlite_version_ok" in h
    assert h["api_version"]


# --------------------------------------------------------------- bonus: events audit + snooze + utf8
def test_events_audit_and_utf8(db):
    iid = jout(run(["add", "--title", "中文标题 milk", "--source", "email-monitor"], db))["item"]["id"]
    run(["transition", "--id", iid, "--to", "doing"], db)
    run(["done", "--id", iid], db)
    evs = jout(run(["events", "--id", iid], db))["events"]
    types = [e["event_type"] for e in evs]
    assert "created" in types and "status_change" in types
    got = jout(run(["get", "--id", iid], db))["item"]
    assert got["title"] == "中文标题 milk"  # UTF-8 round-trips through the contract


def test_snooze_suppresses_due(db, tmp_path):
    iid = jout(run(["add", "--title", "sn", "--due-at", "2026-01-01T00:00:00Z"], db))["item"]["id"]
    run(["snooze", "--id", iid, "--until", "2026-12-01T00:00:00Z"], db)
    stub, log = make_stub(tmp_path)
    res = jout(run(["tick", "--now", "2026-06-25T12:00:00Z"], db, {"SCHEDULE_RELAY_CMD": stub}))
    assert iid not in res["dispatched"]  # snoozed past now -> suppressed


# --------------------------------------------------------------- E14 RRULE rolling expansion (§2.4)
def test_e14_recurrence_rolling(db, tmp_path):
    # a long-overdue DAILY item fires ONCE now, then rolls to the next future occurrence (re-arms),
    # never materialising the infinite series. Guards the rolling-next-due capability.
    iid = jout(run(["add", "--title", "daily", "--due-at", "2026-06-20T00:00:00Z",
                    "--recurrence", "FREQ=DAILY;INTERVAL=1"], db))["item"]["id"]
    stub, log = make_stub(tmp_path)
    res = jout(run(["tick", "--now", "2026-06-25T12:00:00Z"], db, {"SCHEDULE_RELAY_CMD": stub}))
    assert iid in res["dispatched"]
    item = jout(run(["get", "--id", iid], db))["item"]
    assert item["notified_at"] is None  # re-armed, not permanently notified
    assert item["due_at"] == "2026-06-26T00:00:00.000000+00:00"  # next occurrence after now
    # same-clock re-tick must NOT re-dispatch (next due is in the future) -> at-least-once preserved
    res2 = jout(run(["tick", "--now", "2026-06-25T12:00:00Z"], db, {"SCHEDULE_RELAY_CMD": stub}))
    assert iid not in res2["dispatched"]
    with open(log, encoding="utf-8") as f:
        assert len([ln for ln in f if ln.strip()]) == 1  # delivered exactly once this period
    # advancing the clock past the next occurrence fires the recurrence again
    res3 = jout(run(["tick", "--now", "2026-06-26T12:00:00Z"], db, {"SCHEDULE_RELAY_CMD": stub}))
    assert iid in res3["dispatched"]


def test_e14_recurrence_until_stops(db, tmp_path):
    # once UNTIL is passed the rule is exhausted: it fires once, then is permanently notified (no roll)
    iid = jout(run(["add", "--title", "u", "--due-at", "2026-06-20T00:00:00Z",
                    "--recurrence", "FREQ=DAILY;UNTIL=20260622T000000Z"], db))["item"]["id"]
    stub, log = make_stub(tmp_path)
    res = jout(run(["tick", "--now", "2026-06-25T12:00:00Z"], db, {"SCHEDULE_RELAY_CMD": stub}))
    assert iid in res["dispatched"]
    item = jout(run(["get", "--id", iid], db))["item"]
    assert item["notified_at"] is not None  # exhausted -> terminal notify, did not re-arm


# --------------------------------------------------------------- E15 alarms[] per-alarm lead (§4.5)
def test_e15_alarm_lead(db, tmp_path):
    # an item with an alarm lead becomes due *before* its due_at; one without does not.
    lead_id = jout(run(["add", "--title", "lead", "--due-at", "2026-06-25T13:00:00Z",
                        "--alarms", '[{"lead":3600}]'], db))["item"]["id"]
    ctl_id = jout(run(["add", "--title", "noalarm", "--due-at", "2026-06-25T13:00:00Z"], db))["item"]["id"]
    stub, log = make_stub(tmp_path)
    # now=12:30: lead item (due-3600 = 12:00 <= 12:30) fires; control (due 13:00 > 12:30) does not
    res = jout(run(["tick", "--now", "2026-06-25T12:30:00Z"], db, {"SCHEDULE_RELAY_CMD": stub}))
    assert lead_id in res["dispatched"]
    assert ctl_id not in res["dispatched"]


def test_e15_alarm_trigger_ical(db):
    # iCalendar VALARM trigger form '-PT1H' is equivalent to a 3600s lead (read-only `due` path).
    iid = jout(run(["add", "--title", "cal", "--due-at", "2026-06-25T13:00:00Z",
                    "--alarms", '[{"trigger":"-PT1H"}]'], db))["item"]["id"]
    res = jout(run(["due", "--now", "2026-06-25T12:30:00Z"], db))
    assert any(i["id"] == iid for i in res["items"])
    # 11:59 is before due-1H (12:00) -> not yet due
    res2 = jout(run(["due", "--now", "2026-06-25T11:59:00Z"], db))
    assert all(i["id"] != iid for i in res2["items"])


# --------------------------------------------------------- concurrent tick: exclusive claim (no dup)
def test_concurrent_tick_exclusive_claim(db, tmp_path):
    # A second tick overlapping the first must NOT re-dispatch an item the first already claimed.
    # We simulate the concurrent peer by stamping a *fresh* claimed_at, then assert this tick skips
    # it; a *stale* claim (crashed tick) is reclaimed. Guards cross-process at-most-... dedupe.
    iid = jout(run(["add", "--title", "c", "--due-at", "2026-01-01T00:00:00Z"], db))["item"]["id"]
    stub, log = make_stub(tmp_path)
    now = "2026-06-25T12:00:00Z"

    def set_claim(ts):
        con = sqlite3.connect(db)
        try:
            con.execute("UPDATE items SET claimed_at=? WHERE id=?", (ts, iid))
            con.commit()
        finally:
            con.close()

    set_claim("2026-06-25T12:00:00.000000+00:00")  # fresh claim by a concurrent tick
    res = jout(run(["tick", "--now", now], db, {"SCHEDULE_RELAY_CMD": stub}))
    assert iid not in res["dispatched"]  # exclusive claim respected -> no double fire
    assert iid in res["skipped"] or iid not in res["dispatched"]

    set_claim("2026-06-25T11:00:00.000000+00:00")  # stale claim (> _CLAIM_TTL ago) -> reclaim
    res2 = jout(run(["tick", "--now", now], db, {"SCHEDULE_RELAY_CMD": stub}))
    assert iid in res2["dispatched"]

    with open(log, encoding="utf-8") as f:
        assert len([ln for ln in f if ln.strip()]) == 1  # delivered exactly once across both ticks


# --------------------------------------------------------------- progress invariant 0-100 clamped
def test_progress_bounds(db):
    iid = jout(run(["add", "--title", "p"], db))["item"]["id"]
    for bad in ("99999", "-1", "101"):
        r = run(["update", "--id", iid, "--set", "progress=%s" % bad], db, check=False)
        assert r.returncode != 0 and jerr(r)["error_code"] == "ERR_BAD_PROGRESS"
    r = run(["add", "--title", "p2", "--progress", "150"], db, check=False)
    assert r.returncode != 0 and jerr(r)["error_code"] == "ERR_BAD_PROGRESS"
    r = run(["transition", "--id", iid, "--to", "doing", "--progress", "200"], db, check=False)
    assert r.returncode != 0 and jerr(r)["error_code"] == "ERR_BAD_PROGRESS"
    ok = jout(run(["update", "--id", iid, "--set", "progress=50"], db))["item"]
    assert ok["progress"] == 50  # valid still applies
