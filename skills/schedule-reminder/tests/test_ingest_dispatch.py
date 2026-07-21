# -*- coding: utf-8 -*-
"""Inbound bus (ingest + dispatch) — safety-critical unit tests.

Focus: the anti-hallucination executor (only act on ids that were shown to the model), JSON plan
extraction robustness, thread-key collision avoidance, and the user-vs-bot ingest filter. All model
and network I/O is stubbed — no codex, no Discord, no real pool.
"""
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import dispatch  # noqa: E402
import ingest    # noqa: E402


# --------------------------------------------------------------------------- _extract_json
@pytest.mark.parametrize("text,expect", [
    ('{"actions":[],"confirm":"x"}', {"actions": [], "confirm": "x"}),
    ('```json\n{"a":1}\n```', {"a": 1}),
    ('```\n{"a":1}\n```', {"a": 1}),
    ('here is the plan: {"actions":[{"op":"done","id":"z"}],"confirm":"ok"} -- done', None),  # see below
])
def test_extract_json_basic(text, expect):
    got = dispatch._extract_json(text)
    if expect is None:
        # prose-wrapped: must still recover the embedded object (not literally None)
        assert got is not None and got.get("confirm") == "ok"
    else:
        assert got == expect


def test_extract_json_garbage_is_none():
    assert dispatch._extract_json("no json here at all") is None
    assert dispatch._extract_json("") is None
    assert dispatch._extract_json(None) is None


def test_extract_json_nested_braces():
    got = dispatch._extract_json('{"actions":[{"op":"create","title":"a{b}c"}],"confirm":"y"}')
    assert got["actions"][0]["title"] == "a{b}c"


# --------------------------------------------------------------------------- _thread_key
def test_thread_key_distinct_chinese_titles():
    k1 = dispatch._thread_key("需回复:房东的门禁卡邮件")
    k2 = dispatch._thread_key("待办:约牙医洗牙")
    assert k1 != k2, "distinct Chinese titles must not collide (the manual:task bug)"
    assert k1.startswith("manual:")


def test_thread_key_stable():
    assert dispatch._thread_key("同一个标题") == dispatch._thread_key("同一个标题")


# --------------------------------------------------------------------------- executor anti-hallucination
def _rem_recorder():
    calls = []

    def fake_rem(*args):
        calls.append(args)
        if args[0] == "done":
            return {"item": {"state": "done"}}
        return {}  # snooze/add success (no _err)
    return calls, fake_rem


def test_execute_only_touches_shown_ids(monkeypatch):
    items = [{"id": "real-1", "title": "genuine item"}]
    calls, fake = _rem_recorder()
    monkeypatch.setattr(dispatch, "_rem", fake)
    plan = {"actions": [
        {"op": "done", "id": "real-1"},       # valid
        {"op": "done", "id": "hallucinated-9"},  # NOT in items -> must be skipped, never sent to _rem
        {"op": "snooze", "id": "ghost-8", "until": "2026-08-01T00:00:00Z"},  # invalid id
        {"op": "create", "title": "待办:新任务"},
    ]}
    res = dispatch.execute("mail", dispatch.STREAMS["mail"], plan, items)
    done_ids = [c[2] for c in calls if c[0] == "done"]
    assert done_ids == ["real-1"], "executor must never send a hallucinated id to reminder.py"
    assert not any(c[0] == "snooze" for c in calls), "invalid snooze id must not reach reminder.py"
    assert res["done"] == 1 and res["created"] == 1
    assert any("hallucinated-9"[:8] in s for s in res["skipped"])
    assert any("ghost-8"[:8] in s for s in res["skipped"])


def test_execute_snooze_requires_until(monkeypatch):
    items = [{"id": "real-1", "title": "x"}]
    calls, fake = _rem_recorder()
    monkeypatch.setattr(dispatch, "_rem", fake)
    plan = {"actions": [{"op": "snooze", "id": "real-1"}]}  # no 'until'
    res = dispatch.execute("reminders", dispatch.STREAMS["reminders"], plan, items)
    assert res["snoozed"] == 0
    assert not any(c[0] == "snooze" for c in calls)


def test_execute_create_source_by_kind(monkeypatch):
    calls, fake = _rem_recorder()
    monkeypatch.setattr(dispatch, "_rem", fake)
    # generic stream -> source agent-center:<stream>, no email-monitor ext
    dispatch.execute("crypto", dispatch.STREAMS["crypto"],
                     {"actions": [{"op": "create", "title": "待办:看链上"}]}, [])
    add = [c for c in calls if c[0] == "add"][0]
    assert "agent-center:crypto" in add
    assert "email-monitor" not in add
    # mail (pool) -> source email-monitor + thread_key ext
    calls.clear()
    dispatch.execute("mail", dispatch.STREAMS["mail"],
                     {"actions": [{"op": "create", "title": "待办:回邮件"}]}, [])
    add = [c for c in calls if c[0] == "add"][0]
    assert "email-monitor" in add
    assert any("x_email_monitor_thread_key" in str(x) for x in add)


# --------------------------------------------------------------------------- dispatch flow (stubbed)
def test_dispatch_happy_path(monkeypatch):
    monkeypatch.setattr(dispatch, "get_state", lambda cfg: [{"id": "i1", "title": "t1"}])
    monkeypatch.setattr(dispatch, "call_chain",
                        lambda *a, **k: '{"actions":[{"op":"done","id":"i1"}],"confirm":"完成1项"}')
    _, fake = _rem_recorder()
    monkeypatch.setattr(dispatch, "_rem", fake)
    posted = []
    monkeypatch.setattr(dispatch.relay, "relay", lambda stream, text: posted.append((stream, text)) or True)
    ok = dispatch.dispatch("mail", "i1 那条搞定了")
    assert ok is True
    assert posted and posted[0][0] == "mail" and "完成" in posted[0][1]


def test_dispatch_unparseable_plan_passthrough(monkeypatch):
    monkeypatch.setattr(dispatch, "get_state", lambda cfg: [])
    monkeypatch.setattr(dispatch, "call_chain", lambda *a, **k: "sorry i cannot help")
    posted = []
    monkeypatch.setattr(dispatch.relay, "relay", lambda stream, text: posted.append((stream, text)) or True)
    ok = dispatch.dispatch("support", "some reply")
    assert ok is False
    assert posted and "自动解析失败" in posted[0][1]


def test_dispatch_no_post_is_dry(monkeypatch):
    monkeypatch.setattr(dispatch, "get_state", lambda cfg: [])
    monkeypatch.setattr(dispatch, "call_chain",
                        lambda *a, **k: '{"actions":[],"confirm":"noop"}')
    called = []
    monkeypatch.setattr(dispatch.relay, "relay", lambda *a, **k: called.append(a) or True)
    dispatch.dispatch("infra", "hi", post=False)
    assert called == [], "post=False must not hit relay"


# --------------------------------------------------------------------------- ingest user-vs-bot filter
def test_ingest_is_user_filters_bots_and_webhooks():
    assert ingest._is_user({"author": {"bot": False}}) is True
    assert ingest._is_user({"author": {"bot": True}}) is False           # a bot (Big Brother confirm)
    assert ingest._is_user({"author": {}, "webhook_id": "123"}) is False  # a webhook (relay push)
    assert ingest._is_user({"author": {"bot": False}, "webhook_id": None}) is True


def test_ingest_streams_respects_inbound_flag():
    reg = {"streams": {
        "a": {"channel_id": "1"},
        "b": {"channel_id": "2", "inbound": False},   # opted out
        "c": {"webhook": "x"},                          # no channel_id -> not pollable
    }}
    got = ingest._streams(reg)
    assert got == {"a": "1"}


# --------------------------------------------------------------------------- reactions (emoji replies)
def test_emoji_ref_unicode_and_custom():
    assert ingest._emoji_ref({"name": "✅", "id": None}) == ("✅", "%E2%9C%85")
    disp, ref = ingest._emoji_ref({"name": "party", "id": "123"})
    assert disp == ":party:" and ref == "party:123"


def test_reaction_events_owner_only(monkeypatch):
    msgs = [{"id": "m1", "content": "待办:交周报", "timestamp": "t",
             "reactions": [{"emoji": {"name": "✅", "id": None}, "count": 2, "me": True}]}]
    monkeypatch.setattr(ingest, "_reactors",
                        lambda ch, mid, ref, tok, limit=100: [{"id": "OWNER", "bot": False},
                                                              {"id": "BOTX", "bot": True}])
    events, keys = ingest.reaction_events("ch", "tok", "OWNER", msgs)
    assert len(events) == 1
    assert events[0]["emoji"] == "✅" and events[0]["message_id"] == "m1"
    assert events[0]["key"] == "m1:✅:OWNER"
    assert keys == {"m1:✅:OWNER"}


def test_reaction_events_skips_bot_only_no_fetch(monkeypatch):
    # only the bot itself reacted (count=1, me=True): others<=0 -> never even fetch reactors
    msgs = [{"id": "m1", "content": "x",
             "reactions": [{"emoji": {"name": "✅", "id": None}, "count": 1, "me": True}]}]
    called = []
    monkeypatch.setattr(ingest, "_reactors", lambda *a, **k: called.append(1) or [])
    events, keys = ingest.reaction_events("ch", "tok", "OWNER", msgs)
    assert events == [] and keys == set() and called == []


def test_reaction_events_wrong_user_filtered(monkeypatch):
    msgs = [{"id": "m1", "content": "x", "timestamp": "t",
             "reactions": [{"emoji": {"name": "✅", "id": None}, "count": 1, "me": False}]}]
    monkeypatch.setattr(ingest, "_reactors", lambda *a, **k: [{"id": "SOMEONE_ELSE", "bot": False}])
    events, _ = ingest.reaction_events("ch", "tok", "OWNER", msgs)
    assert events == []  # a reaction by someone who is not the owner is ignored


def test_poll_reactions_dedups_across_ticks(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    msgs = [{"id": "m1", "content": "待办:交周报", "timestamp": "2026-01-01T00:00:00Z",
             "reactions": [{"emoji": {"name": "✅", "id": None}, "count": 1, "me": False}]}]
    monkeypatch.setattr(ingest, "_fetch", lambda ch, tok, after=None, limit=50: msgs)
    monkeypatch.setattr(ingest, "_reactors", lambda *a, **k: [{"id": "OWNER", "bot": False}])
    new1 = ingest.poll_reactions_stream("mail", "ch", "tok", "OWNER")
    assert len(new1) == 1
    assert os.path.exists(ingest._reactions_inbox_file("mail"))
    assert "交周报" in open(ingest._reactions_inbox_file("mail"), encoding="utf-8").read()
    new2 = ingest.poll_reactions_stream("mail", "ch", "tok", "OWNER")  # same reaction, next tick
    assert new2 == []  # already seen -> not re-processed


def test_arm_reactions_baselines_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    msgs = [{"id": "m1", "content": "x", "timestamp": "t",
             "reactions": [{"emoji": {"name": "✅", "id": None}, "count": 1, "me": False}]}]
    monkeypatch.setattr(ingest, "_fetch", lambda ch, tok, after=None, limit=50: msgs)
    monkeypatch.setattr(ingest, "_reactors", lambda *a, **k: [{"id": "OWNER", "bot": False}])
    reg = {"streams": {"mail": {"channel_id": "ch"}}, "big_brother": {"user_id": "OWNER"}}
    ingest.arm_reactions(reg, "tok")
    # after arming, a poll finds nothing new (existing reaction already baselined as seen)
    assert ingest.poll_reactions_stream("mail", "ch", "tok", "OWNER") == []
