# -*- coding: utf-8 -*-
"""An overdue list only works if being on it is rare, and the sweep must not tidy away a message
nobody received.

Measured 2026-09-05: 72 items were overdue at once, the oldest from mid-July, and among them sat a
confirmation due that same day. Seventy-two red flags is the same signal as none -- the one that
mattered was indistinguishable from a car wash nobody did two months earlier. So the sweep exists.

Its whole risk is in the other direction. Closing an item that was never DELIVERED is not tidying,
it is deleting a message the person never got, and this store already contains proof that delivery
fails: an item titled "task-health digest UNDELIVERED (relay rc=1)". Every test below that says
"must NOT be touched" is guarding a way of losing something, not a style preference.
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import store  # noqa: E402

NOW = "2026-09-05T12:00:00Z"
LONG_AGO = "2026-08-01T00:00:00Z"


def _db(tmp_path):
    p = str(tmp_path / "t.sqlite3")
    store.init_db(db_path=p)
    return p


def _mk(db, title, due, notified=True, state="pending", rec=None):
    item = store.add_item(title, due_at=due, db_path=db, source="test", recurrence=rec)
    con = sqlite3.connect(db)
    con.execute("UPDATE items SET notified_at=?, state=? WHERE id=?",
                (LONG_AGO if notified else None, state, item["id"]))
    con.commit()
    con.close()
    return item["id"]


def _state(db, item_id):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT state FROM items WHERE id=?", (item_id,)).fetchone()[0]
    finally:
        con.close()


def test_delivered_and_long_overdue_is_closed(tmp_path):
    db = _db(tmp_path)
    i = _mk(db, "came and went", LONG_AGO, notified=True)
    res = store.sweep_lapsed(now=NOW, db_path=db)
    assert _state(db, i) == "cancelled"
    assert [x["id"] for x in res["lapsed"]] == [i]


def test_closed_as_cancelled_never_as_done(tmp_path):
    """`done` would assert the task was completed. Nobody checked that, and it is usually false."""
    db = _db(tmp_path)
    i = _mk(db, "nobody did this", LONG_AGO, notified=True)
    store.sweep_lapsed(now=NOW, db_path=db)
    assert _state(db, i) == "cancelled", "a lapsed reminder must not claim the task got done"


def test_undelivered_is_reported_and_left_alone(tmp_path):
    """The line this sweep will not cross."""
    db = _db(tmp_path)
    i = _mk(db, "never reached anyone", LONG_AGO, notified=False)
    res = store.sweep_lapsed(now=NOW, db_path=db)
    assert _state(db, i) == "pending", "closing an undelivered item deletes a message, not a chore"
    assert [x["id"] for x in res["undelivered"]] == [i]
    assert not res["lapsed"]


def test_inside_the_grace_window_is_left_alone(tmp_path):
    """A reminder fires AT its due time; closing it the same day takes it off the list before the
    person it was for has had a chance to look."""
    db = _db(tmp_path)
    i = _mk(db, "due yesterday", "2026-09-04T00:00:00Z", notified=True)
    res = store.sweep_lapsed(now=NOW, db_path=db)
    assert _state(db, i) == "pending"
    assert not res["lapsed"] and not res["undelivered"]


def test_doing_and_blocked_are_left_alone(tmp_path):
    db = _db(tmp_path)
    a = _mk(db, "picked up", LONG_AGO, notified=True, state="doing")
    b = _mk(db, "parked on purpose", LONG_AGO, notified=True, state="blocked")
    store.sweep_lapsed(now=NOW, db_path=db)
    assert _state(db, a) == "doing", "lapsing this would erase work in progress"
    assert _state(db, b) == "blocked", "it is parked with a reason; the sweep is not the un-parker"


def test_recurring_is_left_alone(tmp_path):
    """Recurring items re-arm rather than expire, so a past due_at is normal, not an expiry."""
    db = _db(tmp_path)
    i = _mk(db, "weekly", LONG_AGO, notified=True, rec="FREQ=WEEKLY")
    store.sweep_lapsed(now=NOW, db_path=db)
    assert _state(db, i) == "pending"


def test_dry_run_reports_without_writing(tmp_path):
    db = _db(tmp_path)
    i = _mk(db, "would be closed", LONG_AGO, notified=True)
    res = store.sweep_lapsed(now=NOW, db_path=db, dry_run=True)
    assert [x["id"] for x in res["lapsed"]] == [i]
    assert _state(db, i) == "pending", "a dry run that writes is not a dry run"


def test_tick_carries_the_sweep(tmp_path):
    """The sweep rides the tick, so the existing schedule picks it up with no new task to register,
    monitor and back up. If this ever stops being true the sweep silently never runs."""
    db = _db(tmp_path)
    i = _mk(db, "came and went", LONG_AGO, notified=True)
    res = store.tick(now=NOW, db_path=db, notify_fn=lambda item: True)
    assert "lapsed" in res and "undelivered" in res, "tick stopped reporting the sweep"
    assert [x["id"] for x in res["lapsed"]] == [i]
    assert _state(db, i) == "cancelled"
