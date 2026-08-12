#!/usr/bin/env python3
"""schedule-reminder — Agent Center multi-stream INGEST (the single Discord ingress).

The inbound mirror of relay.py. relay.py POSTs skill output to each stream's webhook (out);
this module GETs each stream's channel for new USER replies (in). Webhooks are send-only, but a
bot with read access can pull channel history over REST (no privileged Message Content Intent).

ONE ANSWER TO "WHICH CHANNELS DO WE READ" (see channels())
    The bus reads every registered stream AND every other readable text channel in the guild. That
    second half is not a convenience, it is the fix for a specific failure. Two readers used to
    disagree about this question: ingest polled the registry whitelist, while the backdrop bot
    discovered the guild. The server's own default channel was in one list and not the other, so an
    instruction typed there was read by the bot that only understood `gradient`, discarded for not
    matching, and had the cursor advanced past it. Nothing else ever looked. The message did not
    fail to be handled, it failed to be SEEN, which leaves no trace anywhere.

    So: one enumeration, one cursor per channel, and the invariant in poll_stream, which is that a
    message the bus read is either claimed by a handler or written to an inbox. Never neither.

REGISTRY (secret; env AGENT_CENTER_CONFIG, else the registry file in the Agent Center config dir)
    streams.<name>.channel_id   -- required to poll a REGISTERED stream (absent -> not pollable)
    streams.<name>.inbound      -- optional; false = the bus does not read this channel at all, and
                                   guild discovery may not add it back (an archive the owner keeps
                                   for themselves, or a reference channel full of example commands)
    reader.bot_token            -- the Discord bot token (canonical; same one relay/bigbrother use)

STATE (the Agent Center state dir)
    <channel_id>.last  -- last processed message id, keyed on the CHANNEL not the stream name.
                          A discovered channel's name is whatever a human typed (emoji, spaces, a
                          slash) and a rename would orphan a name-keyed cursor, which re-reads that
                          channel's history and re-answers old messages. Cursors written under the
                          older name-keyed scheme are adopted on first sight, see _adopt_cursor.
    <key>.inbox        -- newest batch of user replies (consumed by dispatch); <key> is the stream
                          name for registered streams, the channel id for discovered ones.

CLI
    ingest.py poll              # poll all streams; JSON {stream: n_new}; writes inboxes
    ingest.py arm               # set every stream's last id to current latest (no back-processing)
    ingest.py inbox --stream S  # print the pending inbox for a stream
    ingest.py list              # streams + channel_id + inbound flag (NO secrets)

SECRETS: never logs/prints the bot token or webhook URLs. Stdlib only.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

_UA = "AgentCenter-Ingest/1.0 (+https://discord.com)"
_API = "https://discord.com/api/v10"
# Populated by poll_all(): {stream: (channel_id, [msg_id, ...])} for the messages picked up this
# tick, so the caller can ack them with a reaction. Side-channel, not part of poll_all's return.
LAST_POLL_IDS = {}
# {stream: channel_id} for everything enumerated this tick, so a caller can answer IN the channel a
# message came from. Without it a discovered channel's confirmation has no webhook to go to and
# lands in a DM, which reads as the bot ignoring you.
CHANNEL_OF = {}
# {stream: (channel_id, [message, ...])} for this tick. The tick needs the MESSAGES, not just their
# ids, because a command handler matches one message at a time: matching against the whole batch
# would let an ordinary sentence that happens to contain a trigger word run a command.
LAST_POLL_MSGS = {}
_DEFAULT_REGISTRY = os.path.join(os.path.expanduser("~"), ".agent-center", "registry.json")
_STATE_DIR = os.path.join(os.path.expanduser("~"), ".agent-center", "state")


def registry_path():
    return os.environ.get("AGENT_CENTER_CONFIG") or _DEFAULT_REGISTRY


def load_registry():
    try:
        with open(registry_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        sys.stderr.write("ingest: registry unreadable (%s)\n" % e)
        return {}


def bot_token(reg):
    return (reg.get("reader") or {}).get("bot_token")


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bot %s" % token, "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch(channel_id, token, after=None, limit=50):
    q = {"limit": limit}
    if after:
        q["after"] = after
    return _get("%s/channels/%s/messages?%s" % (_API, channel_id, urllib.parse.urlencode(q)), token)


def _is_user(m, owner=None):
    """A genuine human reply: not a bot, not a webhook post (our own alerts/confirmations).

    With `owner` given, it must ALSO be that specific person. A reply can now enqueue real execution
    on this machine, so "any human in the channel" is the wrong audience; the reaction path has
    always narrowed to the owner and the text path used not to. Callers that hold a registry pass
    the owner and MUST NOT fall back to None when it is unset (see poll_all): an unresolvable owner
    has to close this gate, not open it."""
    a = m.get("author") or {}
    if a.get("bot", False) or m.get("webhook_id"):
        return False
    if owner is not None and str(a.get("id") or "") != str(owner):
        return False
    return True


def _last_file(channel_id):
    """The cursor, keyed on the channel id. See the STATE note in the module docstring."""
    return os.path.join(_STATE_DIR, "%s.last" % channel_id)


def _inbox_file(stream):
    return os.path.join(_STATE_DIR, "%s.inbox" % stream)


_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")


def _key(stream, channel_id):
    """Filename-safe state key: the stream name when it is one, else the channel id.

    Registered streams keep their historical file names (mail.inbox stays mail.inbox, and
    `dispatch.py --stream mail` keeps working). A discovered channel is named by whatever the human
    typed, which is not a filename, so it falls back to its id."""
    return stream if stream and _SAFE_KEY.match(stream) else str(channel_id)


def _streams(reg):
    """Registered, pollable streams: {name: channel_id}. The registry half of channels()."""
    out = {}
    for name, s in (reg.get("streams") or {}).items():
        if s.get("channel_id") and s.get("inbound", True):
            out[name] = s["channel_id"]
    return out


def _opted_out(reg):
    """Channel ids the owner has explicitly excluded, so discovery cannot add them back."""
    return {str(s["channel_id"]) for s in (reg.get("streams") or {}).values()
            if s.get("channel_id") and not s.get("inbound", True)}


def discovered_channels(reg, token, log=None):
    """Readable guild text channels that are not registered and not opted out: [(name, id), ...].

    A discovery failure returns nothing rather than raising: it must cost the discovered channels
    only, never the registered ones the caller already holds."""
    gid = reg.get("guild_id")
    if not gid:
        return []
    known = {str(c) for c in _streams(reg).values()} | _opted_out(reg)
    try:
        chans = _get("%s/guilds/%s/channels" % (_API, gid), token)
    except Exception as e:
        if log:
            log("ingest: guild channel discovery failed (%s); registered streams unaffected"
                % type(e).__name__)
        return []
    out = []
    for c in chans or []:
        cid = str(c.get("id"))
        if c.get("type") == 0 and cid not in known:      # 0 = a normal text channel
            out.append(("#%s" % (c.get("name") or cid), cid))
    return out


def owner_dm_channel(reg, token):
    """The DM channel with the operator, opened if it does not exist yet, or None.

    A bot's own DM is a natural place to type at it, and the backdrop bot accepted commands there
    before the readers were merged. Returns None rather than raising: a DM being unreachable must
    cost the DM, not the guild sweep."""
    uid = (reg.get("big_brother") or {}).get("user_id")
    if not uid:
        return None
    try:
        req = urllib.request.Request(
            "%s/users/@me/channels" % _API, method="POST",
            data=json.dumps({"recipient_id": str(uid)}).encode("utf-8"),
            headers={"Authorization": "Bot %s" % token, "User-Agent": _UA,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return str(json.loads(r.read().decode("utf-8"))["id"])
    except Exception:
        return None


def channels(reg, token=None, discover=True, log=None):
    """[(stream, channel_id), ...] for every channel the bus reads. THE single enumeration.

    Registered streams first (their configured names and per-stream dispatch behaviour), then any
    other readable text channel in the guild, so a channel created next month works without anyone
    remembering to register it, and so a message typed in the obvious place is never invisible.
    Finally the operator's DM, which is where the daily digest lands and therefore where a reply to
    it gets typed."""
    out = [(name, str(ch)) for name, ch in _streams(reg).items()]
    if discover and token:
        # Names are only labels, ids are the identity, but the label keys the per-stream result
        # maps, so two channels a human named the same thing must not collapse into one entry.
        used = {name for name, _ in out}
        for name, cid in discovered_channels(reg, token, log=log):
            if name in used:
                name = "%s-%s" % (name, cid[-4:])
            used.add(name)
            out.append((name, cid))
        seen = {c for _, c in out}
        dm = owner_dm_channel(reg, token)
        if dm and dm not in seen:
            out.append(("dm", dm))
    return out


def _adopt_cursor(channel_id, stream):
    """Seed a channel-keyed cursor from the older name-keyed ones, ONCE, before the first poll.

    Returns (adopted, behind): the position taken, and the OTHER scheme's position when the two
    disagreed (else None).

    Two schemes preceded this: ingest wrote <stream>.last, and the backdrop bot wrote
    gradient.<channel_id>.last for the same channel. They ran on different timers, so their
    positions differ, and neither choice is free:

      take the older -> every message in between is replayed through the judgment chain, which can
                        enqueue real work a second time from a reply already acted on;
      take the newer -> those same messages are never processed at all.

    Repeating an action is the worse failure, so this takes the NEWEST and the caller records the
    gap instead of executing it (see poll_stream). Snowflakes are compared numerically: they differ
    in length, so a string compare would order them wrongly."""
    target = _last_file(channel_id)
    if os.path.exists(target):
        return None, None
    found = []
    for cand in (os.path.join(_STATE_DIR, "%s.last" % stream) if stream else None,
                 os.path.join(_STATE_DIR, "gradient.%s.last" % channel_id)):
        if not cand or not os.path.exists(cand):
            continue
        try:
            with open(cand) as f:
                v = f.read().strip()
            int(v)                           # parse BEFORE comparing: an `is None` short circuit
        except (OSError, ValueError):        # would otherwise let a corrupt value through
            continue
        found.append(v)
    if not found:
        return None, None
    best = max(found, key=int)
    behind = min(found, key=int) if len(found) > 1 and min(found, key=int) != best else None
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(target, "w") as f:
        f.write(best)
    return best, behind


def _record_migration_gap(stream, channel_id, token, behind, adopted, log=None):
    """Write down what the cursor merge stepped over, once, for a human to read.

    These messages sat between two disagreeing cursors during the one-time migration. They are NOT
    dispatched: whatever they asked for was most likely already acted on by the reader that was
    ahead, and re-running the judgment chain on them could repeat a real action. But they are also
    not allowed to disappear in silence, because 'a message the bus saw is written down somewhere'
    is the invariant this whole reorganisation is built on, and a migration is exactly when a
    system is most tempted to make an exception to its own rule."""
    try:
        msgs = _fetch(channel_id, token, after=behind)
        missed = [m for m in reversed(msgs) if int(m["id"]) <= int(adopted) and _is_user(m)]
    except Exception as e:
        if log:
            log("ingest: could not read the migration gap for %s (%s)" % (stream, type(e).__name__))
        return
    if not missed:
        return
    path = os.path.join(_STATE_DIR, "%s.migrated.inbox" % _key(stream, channel_id))
    with open(path, "w", encoding="utf-8") as f:
        f.write("(游标迁移:以下 %d 条消息位于两个旧游标之间,已记录但未自动执行,请人工过目)\n---\n"
                % len(missed))
        f.write(format_messages(missed))
    if log:
        log("ingest: %s -> %d message(s) spanned by the cursor merge, recorded in %s"
            % (stream, len(missed), os.path.basename(path)))


def poll_stream(stream, channel_id, token, owner=None, log=None):
    """Return list of new user-reply message dicts (oldest first); advance the cursor; write inbox.

    THE INVARIANT: whatever this advances the cursor past is either returned to the caller (which
    then dispatches it) or was never the owner's to begin with. There is no third outcome where a
    message is consumed and dropped. A reader that skips what it does not recognise, and advances
    anyway, makes the message disappear with no error and no record; that is the bug this bus was
    reorganised around, and it is why the cursor write and the inbox write live in one function."""
    os.makedirs(_STATE_DIR, exist_ok=True)
    adopted, behind = _adopt_cursor(channel_id, stream)
    if behind:
        _record_migration_gap(stream, channel_id, token, behind, adopted, log=log)
    lf = _last_file(channel_id)
    if not os.path.exists(lf):
        # First sight of a channel: ARM it (record the latest id) and process nothing. Back
        # processing a newly discovered channel would replay its entire visible history through the
        # judgment chain, which can enqueue real work from messages written months ago.
        latest = _fetch(channel_id, token, limit=1)
        if latest:
            with open(lf, "w") as f:
                f.write(latest[0]["id"])
        return []
    with open(lf) as f:
        after = f.read().strip()
    msgs = _fetch(channel_id, token, after=after)
    if not msgs:
        return []
    with open(lf, "w") as f:
        f.write(msgs[0]["id"])  # newest first
    users = [m for m in reversed(msgs) if _is_user(m, owner)]  # oldest first
    if users:
        # The inbox records EVERY message read, including ones a command handler will claim a
        # moment later. It is the durable trace that the bus saw them; the tick decides separately
        # what to forward to the judgment chain.
        with open(_inbox_file(_key(stream, channel_id)), "w", encoding="utf-8") as f:
            f.write(format_messages(users))
    return users


def format_messages(msgs):
    """The inbox rendering of a batch. Shared so what dispatch judges and what the inbox records
    cannot drift apart into two different texts."""
    out = []
    for m in msgs:
        out.append("[%s]\n" % m.get("timestamp", ""))
        if m.get("content"):
            out.append(m["content"] + "\n")
        for att in m.get("attachments", []):
            out.append("<attachment: %s %s>\n" % (att.get("filename"), att.get("url")))
        out.append("---\n")
    return "".join(out)


def poll_all(reg=None, token=None, log=None):
    reg = reg if reg is not None else load_registry()
    token = token or bot_token(reg)
    if not token:
        raise RuntimeError("no bot token: set registry.reader.bot_token in the Agent Center registry")
    # Fail CLOSED on an unresolvable owner. A reply can enqueue execution on this machine, so an
    # unset owner must stop the poll rather than quietly widen it to everyone who can post in the
    # channel. This mirrors the missing-token guard directly above.
    owner = owner_id(reg)
    if not owner:
        raise RuntimeError("no owner: set registry.big_brother.user_id; inbound replies are "
                           "owner-only because they can start real work")
    result = {}
    # Side-channel for the ack-reaction feature: {stream: (channel_id, [msg_id, ...])}. Kept OUT of
    # the return value so poll_all's {stream: count} contract stays intact for existing callers.
    global LAST_POLL_IDS, LAST_POLL_MSGS, CHANNEL_OF
    LAST_POLL_IDS = {}
    LAST_POLL_MSGS = {}
    CHANNEL_OF = {}
    for stream, ch in channels(reg, token, log=log):
        CHANNEL_OF[stream] = ch
        try:
            users = poll_stream(stream, ch, token, owner, log=log)
            if users:
                result[stream] = len(users)
                LAST_POLL_IDS[stream] = (ch, [m["id"] for m in users if m.get("id")])
                LAST_POLL_MSGS[stream] = (ch, users)
                if log:
                    log("ingest: %s -> %d new reply(ies)" % (stream, len(users)))
        except Exception as e:
            if log:
                log("ingest: %s poll error: %s" % (stream, type(e).__name__))
    return result


# ---------------------------------------------------------------- reactions (emoji replies)
# The user often answers a pushed alert by REACTING with an emoji instead of typing. A reaction
# lands on the webhook/bot alert itself (which poll_stream deliberately skips) and creates NO new
# message (so the `after` cursor never sees it). Reactions therefore get their own path: scan
# recent messages, read each message's `reactions`, confirm the OWNER reacted (not the bot),
# dedup against a per-stream seen-set, and synthesize an inbox line dispatch can judge like any
# other reply. Reading reactions over REST does NOT need the Message Content Intent.
#
# Extra STATE (the Agent Center state dir)
#     <stream>.reactions.seen   -- JSON list of processed "msgid:emoji:userid" keys (bounded)
#     <stream>.reactions.inbox  -- newest batch of emoji replies (consumed by dispatch)

def owner_id(reg):
    return str((reg.get("big_brother") or {}).get("user_id") or "").strip() or None


_EMOJI_HINTS = ("✅✔️☑️👍=完成/已处理/确认/是; ❌🚫👎=取消/忽略/否; 👀=已看到; "
                "⏰🔔😴=稍后再提醒(snooze); ❓=需要更多信息")


def _emoji_ref(emoji):
    """(display, api_ref): unicode -> (char, %-quoted char); custom -> (:name:, name:id)."""
    name = emoji.get("name") or ""
    eid = emoji.get("id")
    if eid:
        return (":%s:" % name, "%s:%s" % (name, eid))
    return (name, urllib.parse.quote(name))


def _reactors(channel_id, msg_id, api_ref, token, limit=100):
    try:
        return _get("%s/channels/%s/messages/%s/reactions/%s?limit=%d"
                    % (_API, channel_id, msg_id, api_ref, limit), token)
    except Exception:
        return []


def _reactions_inbox_file(stream):
    return os.path.join(_STATE_DIR, "%s.reactions.inbox" % stream)


def _seen_file(stream):
    return os.path.join(_STATE_DIR, "%s.reactions.seen" % stream)


def _load_seen(stream):
    try:
        with open(_seen_file(stream), encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_seen(stream, seen):
    try:
        with open(_seen_file(stream), "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f)
    except Exception:
        pass


def _snippet(text, n=280):
    t = " ".join((text or "").split())
    return (t[:n] + "…") if len(t) > n else t


def reaction_events(channel_id, token, owner, msgs):
    """(events, all_owner_keys) for owner reactions on `msgs`. Pure of persisted state.
    Each event: {key, message_id, emoji, content, timestamp}. key = 'msgid:emoji:userid'."""
    events, keys = [], set()
    for m in msgs:
        mid = m["id"]
        for rx in (m.get("reactions") or []):
            emoji = rx.get("emoji") or {}
            if rx.get("count", 0) - (1 if rx.get("me") else 0) <= 0:
                continue  # only the bot itself reacted -> nothing from the user
            disp, api_ref = _emoji_ref(emoji)
            ekey = emoji.get("id") or emoji.get("name") or "?"
            for u in _reactors(channel_id, mid, api_ref, token):
                uid = str(u.get("id") or "")
                if u.get("bot") or (owner and uid != owner):
                    continue
                key = "%s:%s:%s" % (mid, ekey, uid)
                keys.add(key)
                events.append({"key": key, "message_id": mid, "emoji": disp,
                               "content": _snippet(m.get("content")), "timestamp": m.get("timestamp", "")})
    return events, keys


def poll_reactions_stream(stream, channel_id, token, owner, limit=50):
    """New owner reactions on recent messages -> write synthesized inbox; return new events."""
    os.makedirs(_STATE_DIR, exist_ok=True)
    stream = _key(stream, channel_id)
    msgs = _fetch(channel_id, token, limit=limit)  # recent, newest first
    if not msgs:
        return []
    seen = _load_seen(stream)
    all_events, all_keys = reaction_events(channel_id, token, owner, msgs)
    window = {m["id"] for m in msgs}
    new = [e for e in all_events if e["key"] not in seen]
    # persist seen bounded to the current window, so it can never grow without limit
    _save_seen(stream, {k for k in (seen | all_keys) if k.split(":", 1)[0] in window})
    if new:
        with open(_reactions_inbox_file(stream), "w", encoding="utf-8") as f:
            f.write("(以下是用户用 emoji 反应回复的, 不是打字。emoji 含义参考: %s)\n---\n" % _EMOJI_HINTS)
            for e in new:
                f.write("[reaction %s] 用户在这条推送上点了「%s」\n" % (e["timestamp"], e["emoji"]))
                if e["content"]:
                    f.write("被反应的推送内容: %s\n" % e["content"])
                f.write("---\n")
    return new


# ------------------------------------------------------- ack reactions (bot -> user, outbound)
# Progress signal on the USER's own message: 👀 the moment a reply is picked up, swapped to ✅ once
# dispatch finished. Without it the judgment chain's ~60-70s of silence is indistinguishable from
# "never received it".
#
# SAFE against self-feedback by construction: reaction_events() subtracts rx["me"] (Discord's
# "this bot reacted" flag) and additionally drops any reactor with u["bot"] or uid != owner. So a
# bot-authored ✅ can never be read back as the owner confirming "done". Do NOT relax those two
# filters without revisiting this.
_ACK_SEEN = "\U0001F440"   # 👀 received, working on it
_ACK_DONE = "✅"       # ✅ finished

def _reaction_call(channel_id, msg_id, emoji, token, method, attempts=3):
    """PUT/DELETE the bot's own reaction (@me). Best effort: never raises, never blocks the tick.

    Retries on 429: back-to-back reaction writes on the SAME message (the ✅-then-remove-👀 swap)
    reliably trip Discord's per-route rate limit, and a silently swallowed 429 used to leave BOTH
    emoji on the message. Honors retry_after when Discord supplies it.
    """
    ref = urllib.parse.quote(emoji)
    url = "%s/channels/%s/messages/%s/reactions/%s/@me" % (_API, channel_id, msg_id, ref)
    for i in range(attempts):
        req = urllib.request.Request(url, method=method,
                                     headers={"Authorization": "Bot %s" % token, "User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status in (200, 204)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < attempts - 1:
                wait = 1.0
                try:
                    wait = float(json.loads(e.read().decode("utf-8")).get("retry_after") or 1.0)
                except Exception:
                    pass
                time.sleep(min(wait + 0.25, 5.0))
                continue
            return False
        except Exception:
            if i < attempts - 1:
                time.sleep(0.5)
                continue
            return False
    return False


def ack_seen(channel_id, msg_id, token):
    """Mark a just-picked-up user message as being worked on."""
    return _reaction_call(channel_id, msg_id, _ACK_SEEN, token, "PUT")


def ack_done(channel_id, msg_id, token):
    """Swap 👀 -> ✅ after dispatch. Add first, then remove, so the message is never un-marked.
    The small gap between the two writes keeps the same-message rate limit from eating the DELETE."""
    ok = _reaction_call(channel_id, msg_id, _ACK_DONE, token, "PUT")
    time.sleep(0.35)
    _reaction_call(channel_id, msg_id, _ACK_SEEN, token, "DELETE")
    return ok


def poll_all_reactions(reg=None, token=None, log=None):
    reg = reg if reg is not None else load_registry()
    token = token or bot_token(reg)
    if not token:
        raise RuntimeError("no bot token: set registry.reader.bot_token in the Agent Center registry")
    owner = owner_id(reg)
    result = {}
    global CHANNEL_OF
    for stream, ch in channels(reg, token, log=log):
        CHANNEL_OF.setdefault(stream, ch)
        try:
            new = poll_reactions_stream(stream, ch, token, owner)
            if new:
                result[stream] = len(new)
                if log:
                    log("ingest: %s -> %d new reaction(s)" % (stream, len(new)))
        except Exception as e:
            if log:
                log("ingest: %s reaction poll error: %s" % (stream, type(e).__name__))
    return result


def arm_reactions(reg, token):
    """Record all current owner reactions as seen so a later poll won't back-process them."""
    owner = owner_id(reg)
    n = 0
    for stream, ch in channels(reg, token):
        try:
            _, keys = reaction_events(ch, token, owner, _fetch(ch, token, limit=50))
            _save_seen(_key(stream, ch), keys)
            n += 1
        except Exception:
            pass
    return n


def main():
    ap = argparse.ArgumentParser(prog="ingest.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll")
    sub.add_parser("reactions")
    sub.add_parser("arm")
    sub.add_parser("list")
    pi = sub.add_parser("inbox")
    pi.add_argument("--stream", required=True)
    a = ap.parse_args()
    reg = load_registry()

    if a.cmd == "list":
        streams = {n: {"channel_id": s.get("channel_id"), "inbound": s.get("inbound", True)}
                   for n, s in (reg.get("streams") or {}).items()}
        out = {"streams": streams}
        # Show what discovery adds too: "registered" and "read" are different sets now, and a
        # listing that only prints the registry hides exactly the channels most likely to surprise.
        try:
            out["discovered"] = [{"name": n, "channel_id": c}
                                 for n, c in discovered_channels(reg, bot_token(reg))]
        except Exception as e:
            out["discovered"] = "unavailable (%s)" % type(e).__name__
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "inbox":
        p = _inbox_file(a.stream)
        print(open(p, encoding="utf-8").read() if os.path.exists(p) else "")
        return 0
    if a.cmd == "arm":
        tok = bot_token(reg)
        os.makedirs(_STATE_DIR, exist_ok=True)
        n = 0
        for stream, ch in channels(reg, tok):
            latest = _fetch(ch, tok, limit=1)
            if latest:
                with open(_last_file(ch), "w") as f:
                    f.write(latest[0]["id"])
                n += 1
        rn = arm_reactions(reg, tok)
        print(json.dumps({"armed": n, "armed_reactions": rn}))
        return 0
    if a.cmd == "poll":
        res = poll_all(reg, log=lambda m: print(m, file=sys.stderr))
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if a.cmd == "reactions":
        res = poll_all_reactions(reg, log=lambda m: print(m, file=sys.stderr))
        print(json.dumps(res, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
