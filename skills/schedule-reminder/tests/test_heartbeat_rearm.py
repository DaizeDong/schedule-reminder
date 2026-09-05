# -*- coding: utf-8 -*-
"""A fixed-key heartbeat must be able to fire more than once in its lifetime.

The failure this pins down: tick() selects on `notified_at IS NULL` and only a recurrence clears
that flag, so a producer that upserts one fixed idempotency key every run (pushing due_at forward
so its own SILENCE becomes the alarm) got exactly ONE notification, ever. Every later upsert wrote
a due_at onto a row tick would never look at again: an update that could not have an effect.

That is how a backstop channel stops speaking without anyone noticing. Measured before the fix:
a delivery-failure reminder had notified_at pinned weeks in the past while its due_at kept
advancing, and it was overdue by days without a single new notification.
"""
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import store  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / "t.sqlite3")
    store.init_db(p)
    return p


def add(db, key, due, title="heartbeat"):
    return store.add_item(title, due_at=due, idempotency_key=key, source="test", db_path=db)


def fire(db, now):
    """One tick with a notifier that always succeeds. Returns the dispatched list."""
    res = store.tick(now=now, notify_fn=lambda *a, **k: True, db_path=db, actor="test")
    return res["dispatched"]


def events_of(db, item_id):
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute(
            "SELECT event_type FROM events WHERE item_id=? ORDER BY rowid", (item_id,))]
    finally:
        conn.close()


def test_heartbeat_fires_again_after_being_pushed_forward(db):
    """The whole point. Fire once, push due_at forward, must be able to fire again.

    Against the old code the second tick dispatched nothing: notified_at stayed set forever.
    """
    add(db, "watchdog", "2026-01-01T00:00:00Z")
    assert len(fire(db, "2026-01-01T00:05:00Z")) == 1, "first fire is the precondition"

    # producer alive: it pushes its own deadline into the future
    add(db, "watchdog", "2026-01-02T00:00:00Z")
    assert fire(db, "2026-01-01T12:00:00Z") == [], "not due yet, must stay quiet"

    # producer went away, nothing pushed it again, the deadline passes
    assert len(fire(db, "2026-01-02T00:05:00Z")) == 1, \
        "a re-armed heartbeat must fire again when its new deadline passes"


def test_replaying_the_same_due_at_does_not_resurrect_an_acknowledged_item(db):
    """Negative control. A caller that merely retries the identical upsert must NOT cause a
    second notification: only moving the deadline forward means 'due again'."""
    add(db, "once", "2026-01-01T00:00:00Z")
    assert len(fire(db, "2026-01-01T00:05:00Z")) == 1
    add(db, "once", "2026-01-01T00:00:00Z")            # identical replay
    assert fire(db, "2026-01-01T06:00:00Z") == [], "an identical replay must not re-arm"


def test_moving_due_at_backwards_does_not_rearm(db):
    """Negative control. Only a strictly later deadline re-arms."""
    add(db, "back", "2026-01-02T00:00:00Z")
    assert len(fire(db, "2026-01-02T00:05:00Z")) == 1
    add(db, "back", "2026-01-01T00:00:00Z")            # earlier than before
    assert fire(db, "2026-01-03T00:00:00Z") == [], "an earlier deadline is not a re-arm"


def test_an_unnotified_item_is_untouched_by_the_rearm_path(db):
    """Negative control. Pushing a not-yet-fired item forward is an ordinary update and must
    keep behaving as one: it fires once, at the new time, not twice."""
    add(db, "pending", "2026-01-01T00:00:00Z")
    add(db, "pending", "2026-01-05T00:00:00Z")         # moved forward before it ever fired
    assert fire(db, "2026-01-02T00:00:00Z") == [], "must respect the new deadline"
    assert len(fire(db, "2026-01-05T00:05:00Z")) == 1


def test_a_done_item_is_not_revived(db):
    """Clearing notified_at must not resurrect something deliberately closed. tick's own state
    filter is what guarantees that, so this pins that the two mechanisms agree."""
    it = add(db, "closed", "2026-01-01T00:00:00Z")
    fire(db, "2026-01-01T00:05:00Z")
    store.done(it["id"], actor="test", db_path=db)
    add(db, "closed", "2026-02-01T00:00:00Z")
    assert fire(db, "2026-02-01T00:05:00Z") == [], "a done item must stay done"


def test_rearm_is_recorded_as_its_own_event(db):
    """An operator can only audit what is written down: a re-arm must be distinguishable from
    an ordinary idempotent replay in the event log."""
    it = add(db, "audited", "2026-01-01T00:00:00Z")
    fire(db, "2026-01-01T00:05:00Z")
    add(db, "audited", "2026-01-02T00:00:00Z")
    acts = events_of(db, it["id"])
    assert "rearmed" in acts, "the re-arm left no audit trail: %r" % (acts,)


def test_an_ordinary_replay_is_still_logged_as_a_replay(db):
    """The two paths must remain distinguishable in both directions."""
    it = add(db, "plain", "2026-06-01T00:00:00Z")
    add(db, "plain", "2026-06-01T00:00:00Z")
    acts = events_of(db, it["id"])
    assert "idempotent_replay" in acts
    assert "rearmed" not in acts
