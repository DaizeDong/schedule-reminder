# -*- coding: utf-8 -*-
"""Inbound bus (ingest + dispatch) — safety-critical unit tests.

Focus: the anti-hallucination executor (only act on ids that were shown to the model), JSON plan
extraction robustness, thread-key collision avoidance, and the user-vs-bot ingest filter. All model
and network I/O is stubbed — no codex, no Discord, no real pool.
"""
import os
import json
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import dispatch  # noqa: E402
import inbound   # noqa: E402
import ingest    # noqa: E402
_REAL_OWNER_DM = ingest.owner_dm_channel  # the autouse fixture stubs it; one case needs the real one
_FIXTURE = json.loads(open(os.path.join(os.path.dirname(__file__), "capability_cases.json"), encoding="utf-8").read())


@pytest.fixture(autouse=True)
def isolate_owner_dm(monkeypatch):
    """Individual DM cases supply their own response; other unit cases stay offline."""
    monkeypatch.setattr(ingest, 'owner_dm_channel', lambda reg, token: None)
    monkeypatch.setattr(dispatch, 'get_work', lambda: [])


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
    k1 = dispatch._thread_key(_FIXTURE["synthetic_thread_titles"][0])
    k2 = dispatch._thread_key(_FIXTURE["synthetic_thread_titles"][1])
    assert k1 != k2, "distinct Chinese titles must not collide (the manual:task bug)"
    assert k1.startswith("manual:")


def test_thread_key_stable():
    assert dispatch._thread_key("同一个标题") == dispatch._thread_key("同一个标题")


# --------------------------------------------------------------------------- executor anti-hallucination
def _store_recorder(monkeypatch):
    """Record the pool mutations dispatch makes in process, without a database."""
    calls = []
    store = dispatch.store
    monkeypatch.setattr(store, "done", lambda iid, **k: calls.append(("done", iid, k))
                        or {"id": iid, "state": "done"})
    monkeypatch.setattr(store, "snooze", lambda iid, until, **k: calls.append(("snooze", iid, k))
                        or {"id": iid, "state": "pending"})
    monkeypatch.setattr(store, "add_item", lambda title, **k: calls.append(("add", title, k))
                        or dict(_FIXTURE["schedule3_created_item"]))
    monkeypatch.setattr(store, "ensure_item", lambda title, **k: calls.append(("ensure", title, k))
                        or {"item": dict(_FIXTURE["schedule3_created_item"]), "decision": "created"})
    return calls


def test_execute_only_touches_shown_ids(monkeypatch):
    items = [{"id": "real-1", "title": "genuine item"}]
    calls = _store_recorder(monkeypatch)
    plan = {"actions": [
        {"op": "done", "id": "real-1"},       # valid
        {"op": "done", "id": "hallucinated-9"},  # NOT in items -> must be skipped, never mutated
        {"op": "snooze", "id": "ghost-8", "until": "2026-08-01T00:00:00Z"},  # invalid id
        {"op": "create", "title": "待办:新任务"},
    ]}
    res = dispatch.execute("mail", dispatch.STREAMS["mail"], plan, items)
    done_ids = [c[1] for c in calls if c[0] == "done"]
    assert done_ids == ["real-1"], "executor must never mutate a hallucinated id"
    assert not any(c[0] == "snooze" for c in calls), "an invalid snooze id must not be mutated"
    assert all(c[2]["actor"] == dispatch.ACTOR for c in calls), "mutations keep their actor"
    assert res["done"] == 1 and res["created"] == 1
    assert any("hallucinated-9"[:8] in s for s in res["skipped"])
    assert any("ghost-8"[:8] in s for s in res["skipped"])


def test_execute_snooze_requires_until(monkeypatch):
    items = [{"id": "real-1", "title": "x"}]
    calls = _store_recorder(monkeypatch)
    plan = {"actions": [{"op": "snooze", "id": "real-1"}]}  # no 'until'
    res = dispatch.execute("reminders", dispatch.STREAMS["reminders"], plan, items)
    assert res["snoozed"] == 0
    assert not any(c[0] == "snooze" for c in calls)


def test_execute_create_source_by_kind(monkeypatch):
    calls = _store_recorder(monkeypatch)
    # generic stream -> source agent-center:<stream>, no email-monitor ext
    dispatch.execute("crypto", dispatch.STREAMS["crypto"],
                     {"actions": [{"op": "create", "title": "待办:看链上"}]}, [])
    add = [c for c in calls if c[0] == "add"][0]
    assert add[2]["source"] == "agent-center:crypto"
    assert "ext" not in add[2]
    # mail (pool) -> source email-monitor + thread_key ext
    calls.clear()
    dispatch.execute("mail", dispatch.STREAMS["mail"],
                     {"actions": [{"op": "create", "title": "待办:回邮件"}]}, [])
    add = [c for c in calls if c[0] == "add"][0]
    assert add[2]["source"] == "email-monitor"
    assert "x_email_monitor_thread_key" in add[2]["ext"]


def test_execute_mutates_the_real_pool_in_process():
    """Done, snooze and an idempotent create reach the database without a reminder.py process,
    and the event log still names dispatch as the actor."""
    store = dispatch.store
    store.init_db()
    first = store.add_item("Acme first", source="agent-center:infra")
    second = store.add_item("Acme second", source="agent-center:infra")
    plan = {"actions": [{"op": "done", "id": first["id"]},
                        {"op": "snooze", "id": second["id"], "until": "2031-01-01T00:00:00Z"},
                        {"op": "create", "title": "待办:Acme follow-up"}]}
    shown = [first, second]
    for _ in range(2):
        result = dispatch.execute("infra", dispatch.STREAMS["infra"], plan, shown, request_id="message-1")
        assert not result["failed"], result
    assert store.get_item(first["id"])["state"] == "done"
    assert store.get_item(second["id"])["wait_until"].startswith("2031-01-01T00:00:00")
    created = [it for it in store.list_items(limit=100)["items"] if it["title"] == "待办:Acme follow-up"]
    assert len(created) == 1, "a replayed create returns the first item"
    assert any(event["actor"] == dispatch.ACTOR for event in store.get_events(first["id"]))


# --------------------------------------------------------------------------- dispatch flow (stubbed)
def test_dispatch_happy_path(monkeypatch):
    monkeypatch.setattr(dispatch, "get_state", lambda cfg: [{"id": "i1", "title": "t1"}])
    monkeypatch.setattr(dispatch, "call_chain",
                        lambda *a, **k: '{"actions":[{"op":"done","id":"i1"}],"confirm":"完成1项"}')
    _store_recorder(monkeypatch)
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


def test_unparseable_plan_is_announced_only_on_the_final_attempt(monkeypatch):
    monkeypatch.setattr(dispatch, "get_state", lambda cfg: [])
    budgets = []
    monkeypatch.setattr(dispatch, "call_chain", lambda *a, **k: budgets.append(k.get("timeout")) or None)
    posted = []
    monkeypatch.setattr(dispatch.relay, "relay", lambda stream, text: posted.append(text) or True)
    assert dispatch.dispatch("support", "some reply", timeout=42, final=False) is False
    assert posted == [], "an attempt that will be retried stays quiet"
    assert budgets == [42], "the caller's time budget bounds the planner call"


def test_rejected_action_completes_with_its_confirmation(monkeypatch):
    """A rejected action is final: its saved outcome would be reused by every retry."""
    monkeypatch.setattr(dispatch, "get_state", lambda cfg: [{"id": "i1", "title": "t1"}])
    monkeypatch.setattr(dispatch, "call_chain",
                        lambda *a, **k: '{"actions":[{"op":"done","id":"not-shown"}]}')
    calls = _store_recorder(monkeypatch)
    posted = []
    monkeypatch.setattr(dispatch.relay, "relay", lambda stream, text: posted.append(text) or True)
    assert dispatch.dispatch("mail", "done with that one") is True
    assert calls == [] and "rejected" in posted[0]


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
    staged = inbound.pending(ingest.state_dir())
    assert len(staged) == 1 and "交周报" in staged[0]["payload"]["content"]
    new2 = ingest.poll_reactions_stream("mail", "ch", "tok", "OWNER")  # same reaction, next tick
    assert new2 == []  # already seen -> not re-processed


def test_unchanged_reaction_count_is_not_read_or_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    msgs = [{"id": "m1", "content": "x", "timestamp": "t",
             "reactions": [{"emoji": {"name": "✅", "id": None}, "count": 1, "me": False}]}]
    reads = []
    monkeypatch.setattr(ingest, "_reactors", lambda *a, **k: reads.append(1) or [{"id": "OWNER", "bot": False}])
    assert len(ingest.poll_reactions_stream("mail", "ch", "tok", "OWNER", messages=msgs)) == 1
    seen = tmp_path / "mail.reactions.seen"
    stamp = seen.stat().st_mtime_ns
    writes = []
    original = ingest._save_seen
    monkeypatch.setattr(ingest, "_save_seen", lambda *a, **k: writes.append(1) or original(*a, **k))
    assert ingest.poll_reactions_stream("mail", "ch", "tok", "OWNER", messages=msgs) == []
    assert reads == [1], "an unchanged count does not read its reactors again"
    assert writes == [] and seen.stat().st_mtime_ns == stamp, "an unchanged seen set is not rewritten"
    msgs[0]["reactions"][0]["count"] = 2
    ingest.poll_reactions_stream("mail", "ch", "tok", "OWNER", messages=msgs)
    assert reads == [1, 1], "a changed count is read again"


def test_failed_reactor_read_is_retried_next_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    msgs = [{"id": "m1", "content": "x", "timestamp": "t",
             "reactions": [{"emoji": {"name": "✅", "id": None}, "count": 1, "me": False}]}]
    monkeypatch.setattr(ingest, "_reactors", lambda *a, **k: None)
    assert ingest.poll_reactions_stream("mail", "ch", "tok", "OWNER", messages=msgs) == []
    monkeypatch.setattr(ingest, "_reactors", lambda *a, **k: [{"id": "OWNER", "bot": False}])
    assert len(ingest.poll_reactions_stream("mail", "ch", "tok", "OWNER", messages=msgs)) == 1


def test_legacy_seen_list_still_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    (tmp_path / "mail.reactions.seen").write_text('["m1:x:OWNER"]', encoding="utf-8")
    assert ingest._load_seen("mail") == ({"m1:x:OWNER"}, {})


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


def test_owner_dm_is_opened_once_and_cached_per_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    opened = []

    class Response:
        def __init__(self, body):
            self.body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self.body

    def urlopen(request, timeout=None):
        opened.append(json.loads(request.data)["recipient_id"])
        return Response(json.dumps({"id": "5550001"}).encode())

    monkeypatch.setattr(ingest.urllib.request, "urlopen", urlopen)
    reg = {"big_brother": {"user_id": "1001"}}
    assert _REAL_OWNER_DM(reg, "TOK") == "5550001"
    assert _REAL_OWNER_DM(reg, "TOK") == "5550001"
    assert opened == ["1001"], "the DM channel id is cached, not reopened every tick"
    assert _REAL_OWNER_DM({"big_brother": {"user_id": "1002"}}, "TOK") == "5550001"
    assert opened == ["1001", "1002"], "a different owner never reuses another owner's DM"


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
    assert inbound.pending(ingest.state_dir()) == []


def test_poll_stream_stages_new_messages_before_moving_the_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    (tmp_path / "88.last").write_text("100")
    monkeypatch.setattr(ingest, "_fetch", lambda ch, tok, after=None, limit=50: [
        {"id": "101", "author": {"bot": False, "id": "OWNER"}, "content": "开个新频道吧",
         "timestamp": "2026-08-10T19:14:00Z"}])
    got = ingest.poll_stream("#常规", "88", "TOK", "OWNER")
    assert len(got) == 1
    staged = inbound.pending(ingest.state_dir())
    assert [row["payload"]["content"] for row in staged] == ["开个新频道吧"]
    assert (tmp_path / "88.last").read_text() == "101"


def _message(identity, content="synthetic"):
    return {"id": str(identity), "author": {"bot": False, "id": "OWNER"}, "content": content,
            "timestamp": "t", "reactions": []}


def test_one_sweep_reads_each_channel_once_for_text_and_reactions(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    for channel in ("1", "2"):
        (tmp_path / ("%s.last" % channel)).write_text("100")
    pages = {"1": [_message(102, "second"), _message(101, "first"), _message(100)],
             "2": [_message(100)]}
    fetches, enumerations = [], []
    def fetch(ch, tok, after=None, limit=50):
        fetches.append((ch, after))
        return pages[ch]
    monkeypatch.setattr(ingest, "_fetch", fetch)
    monkeypatch.setattr(ingest, "channels",
                        lambda reg, token, log=None: enumerations.append(1) or [("mail", "1"), ("infra", "2")])
    monkeypatch.setattr(ingest, "_poll_start", lambda count: 0)
    reg = {"big_brother": {"user_id": "OWNER"}}
    result = ingest.poll(reg, "TOK")
    assert enumerations == [1], "channels are enumerated once per sweep"
    assert fetches == [("1", None), ("2", None)], "each channel is read once"
    assert result == {"text": {"mail": 2}, "reactions": {}}
    assert [row["payload"]["content"] for row in inbound.pending(ingest.state_dir())] == ["first", "second"]
    assert (tmp_path / "1.last").read_text() == "102"


def test_a_full_page_newer_than_the_cursor_reads_from_the_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    (tmp_path / "1.last").write_text("100")
    page = [_message(200 - index) for index in range(50)]           # 200..151, all newer
    gap = [_message(150 - index) for index in range(50)]            # 150..101, after the cursor
    calls = []
    def fetch(ch, tok, after=None, limit=50):
        calls.append(after)
        return gap
    monkeypatch.setattr(ingest, "_fetch", fetch)
    got = ingest.poll_stream("mail", "1", "TOK", "OWNER", page=page)
    assert calls == ["100"], "a page that may skip messages is not trusted as the backlog"
    assert got[0]["id"] == "101" and (tmp_path / "1.last").read_text() == "150"


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


def test_poll_start_channel_rotates_across_ticks(tmp_path, monkeypatch):
    """A sweep cut short by its deadline must not skip the same tail channels every tick."""
    import time as real_time
    from types import SimpleNamespace
    monkeypatch.setattr(ingest, "_STATE_DIR", str(tmp_path))
    fetches = []
    def fetch(ch, tok, after=None, limit=50):
        fetches.append(ch)
        return []
    monkeypatch.setattr(ingest, "_fetch", fetch)
    monkeypatch.setattr(ingest, "channels",
                        lambda reg, token, log=None: [("a", "1"), ("b", "2"), ("c", "3")])
    reg = {"big_brother": {"user_id": "OWNER"}}
    firsts = []
    for now in (0, 300, 600, 900):
        monkeypatch.setattr(ingest, "time",
                            SimpleNamespace(time=lambda now=now: now, monotonic=real_time.monotonic))
        fetches.clear()
        ingest.poll(reg, "TOK")
        order = list(dict.fromkeys(fetches))  # first read of each channel, in sweep order
        assert sorted(order) == ["1", "2", "3"]
        firsts.append(order[0])
    assert firsts == ["1", "2", "3", "1"]
