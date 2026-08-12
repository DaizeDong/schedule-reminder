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


# ------------------------------------------------------- one enumeration (registry + discovery)
def _reg(streams, guild="G"):
    return {"guild_id": guild, "streams": streams, "big_brother": {"user_id": "OWNER"},
            "reader": {"bot_token": "TOK"}}


def _guild(monkeypatch, chans):
    monkeypatch.setattr(ingest, "_get", lambda url, tok: chans)


def test_channels_includes_unregistered_guild_channels(monkeypatch):
    """The bug this whole reorganisation is about: the server's default channel was in nobody's
    list, so a message typed there was never read by anything that could understand it."""
    reg = _reg({"mail": {"channel_id": "1"}})
    _guild(monkeypatch, [{"id": "1", "type": 0, "name": "mail"},
                         {"id": "99", "type": 0, "name": "常规"},
                         {"id": "77", "type": 2, "name": "语音"}])   # voice: not readable text
    got = dict(ingest.channels(reg, "TOK"))
    assert got["mail"] == "1"
    assert got["#常规"] == "99", "an unregistered text channel must still be read"
    assert "99" in got.values() and "77" not in got.values(), "voice channels are not polled"


def test_channels_respects_optout_against_discovery(monkeypatch):
    """inbound:false has to survive discovery, or opting a channel out would be impossible: the
    guild sweep would simply add it back every tick."""
    reg = _reg({"archive": {"channel_id": "42", "inbound": False}})
    _guild(monkeypatch, [{"id": "42", "type": 0, "name": "归档"}])
    assert ingest.channels(reg, "TOK") == [], "an opted-out channel must not return via discovery"


def test_channels_survive_discovery_failure(monkeypatch):
    def boom(url, tok):
        raise OSError("guild listing down")

    monkeypatch.setattr(ingest, "_get", boom)
    got = dict(ingest.channels(_reg({"mail": {"channel_id": "1"}}), "TOK"))
    assert got == {"mail": "1"}, "a discovery failure costs the discovered channels, not the known ones"


def test_channels_disambiguates_duplicate_names(monkeypatch):
    reg = _reg({})
    _guild(monkeypatch, [{"id": "1111", "type": 0, "name": "常规"},
                         {"id": "2222", "type": 0, "name": "常规"}])
    got = ingest.channels(reg, "TOK")
    assert len(got) == 2
    assert len({n for n, _ in got}) == 2, "two channels a human named the same must stay distinct"


def test_channels_includes_the_owner_dm(monkeypatch):
    """The digest lands in the DM, so replies get typed there. It was readable before the two
    readers merged and must not be lost in the merge."""
    reg = _reg({"mail": {"channel_id": "1"}})
    _guild(monkeypatch, [])
    monkeypatch.setattr(ingest, "owner_dm_channel", lambda r, t: "DMCHAN")
    assert ("dm", "DMCHAN") in ingest.channels(reg, "TOK")


def test_unreachable_dm_does_not_break_the_sweep(monkeypatch):
    reg = _reg({"mail": {"channel_id": "1"}})
    _guild(monkeypatch, [])
    monkeypatch.setattr(ingest, "owner_dm_channel", lambda r, t: None)
    assert dict(ingest.channels(reg, "TOK")) == {"mail": "1"}


def test_key_falls_back_to_id_for_unsafe_names():
    assert ingest._key("mail", "1") == "mail"          # registered names keep their state files
    assert ingest._key("#常规", "99") == "99"           # a typed name is not a filename
    assert ingest._key("#a/b", "5") == "5"


# ------------------------------------------------------- cursor adoption (the replay hazard)
def test_adopt_cursor_takes_the_NEWEST_of_the_old_schemes(tmp_path, monkeypatch):
    """Two readers kept two cursors for the same channel. Merging them must take the newest.

    Taking the older one replays every message in between back through the judgment chain, which
    can enqueue real work from stale replies. Snowflakes are compared as numbers on purpose: these
    two differ in length, so a string compare would pick the wrong one."""
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    older, newer = "999999999999999999", "1000000000000000000"   # newer is numerically larger
    assert newer < older, "the string compare is wrong here, which is exactly the trap"
    (tmp_path / "mail.last").write_text(newer)
    (tmp_path / "gradient.77.last").write_text(older)
    adopted, behind = ingest._adopt_cursor("77", "mail")
    assert adopted == newer
    assert behind == older, "the position stepped over must be reported, not forgotten"
    assert (tmp_path / "77.last").read_text() == newer


def test_adopt_cursor_never_overwrites_a_live_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    (tmp_path / "77.last").write_text("500")
    (tmp_path / "mail.last").write_text("900")
    assert ingest._adopt_cursor("77", "mail") == (None, None)
    assert (tmp_path / "77.last").read_text() == "500", "the channel-keyed cursor is authoritative"


def test_adopt_cursor_ignores_corrupt_values(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    (tmp_path / "mail.last").write_text("not-a-snowflake")
    (tmp_path / "gradient.77.last").write_text("123")
    assert ingest._adopt_cursor("77", "mail") == ("123", None)


def test_migration_gap_is_recorded_not_executed(tmp_path, monkeypatch):
    """The one-time cursor merge steps over messages. They must not be dispatched (that could
    repeat an action already taken) and must not vanish either. They get written down."""
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    (tmp_path / "mail.last").write_text("300")          # ingest was here
    (tmp_path / "gradient.77.last").write_text("500")   # the other reader was further ahead
    spanned = [{"id": "500", "author": {"bot": False, "id": "OWNER"}, "content": "第二条",
                "timestamp": "t2"},
               {"id": "400", "author": {"bot": False, "id": "OWNER"}, "content": "被跨过的一条",
                "timestamp": "t1"}]

    def fake_fetch(ch, tok, after=None, limit=50):
        if after == "300":
            return spanned                              # the gap
        if limit == 1:
            return [spanned[0]]
        return []                                       # nothing new after the merge

    monkeypatch.setattr(ingest, "_fetch", fake_fetch)
    got = ingest.poll_stream("mail", "77", "TOK", "OWNER")
    assert got == [], "spanned messages are NOT returned for dispatch"
    rec = (tmp_path / "mail.migrated.inbox").read_text(encoding="utf-8")
    assert "被跨过的一条" in rec and "第二条" in rec, "but they are written down for a human"
    assert (tmp_path / "77.last").read_text() == "500"


def test_first_sight_of_a_channel_arms_and_processes_nothing(tmp_path, monkeypatch):
    """A newly discovered channel must not have its history replayed on the first tick."""
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    history = [{"id": "300", "author": {"bot": False, "id": "OWNER"}, "content": "old instruction"},
               {"id": "200", "author": {"bot": False, "id": "OWNER"}, "content": "older"}]
    monkeypatch.setattr(ingest, "_fetch",
                        lambda ch, tok, after=None, limit=50: history[:limit] if limit == 1 else history)
    got = ingest.poll_stream("#新频道", "88", "TOK", "OWNER")
    assert got == [], "first sight arms the cursor, it does not back-process"
    assert (tmp_path / "88.last").read_text() == "300"
    assert not (tmp_path / "88.inbox").exists()


def test_poll_stream_writes_inbox_under_the_channel_key(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    (tmp_path / "88.last").write_text("100")
    monkeypatch.setattr(ingest, "_fetch", lambda ch, tok, after=None, limit=50: [
        {"id": "101", "author": {"bot": False, "id": "OWNER"}, "content": "开个新频道吧",
         "timestamp": "2026-08-10T19:14:00Z"}])
    got = ingest.poll_stream("#常规", "88", "TOK", "OWNER")
    assert len(got) == 1
    assert "开个新频道吧" in (tmp_path / "88.inbox").read_text(encoding="utf-8")
    assert (tmp_path / "88.last").read_text() == "101"


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
