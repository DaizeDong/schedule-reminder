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
    message the bus read is staged as a durable inbound record before the cursor moves past it.

REGISTRY (secret; env AGENT_CENTER_CONFIG, else the registry file in the Agent Center config dir)
    streams.<name>.channel_id   -- required to poll a REGISTERED stream (absent -> not pollable)
    streams.<name>.inbound      -- optional; false = the bus does not read this channel at all, and
                                   guild discovery may not add it back (an archive the owner keeps
                                   for themselves, or a reference channel full of example commands)
    reader.bot_token_ref        -- cv:// reference to the Discord bot token, resolved through credvault
    reader.bot_token            -- or the token inline (no vault); never both (bot_credential.py)

STATE (the Agent Center state dir)
    <channel_id>.last  -- last processed message id, keyed on the CHANNEL not the stream name.
                          A discovered channel's name is whatever a human typed (emoji, spaces, a
                          slash) and a rename would orphan a name-keyed cursor, which re-reads that
                          channel's history and re-answers old messages.
    owner-dm.channel   -- "<owner id>:<DM channel id>", so the DM is opened once, not every tick.
    inbound/*.json     -- the durable record of every staged message and reaction (inbound.py);
                          inspect those, there is no separate inbox file.

ONE READ PER CHANNEL PER TICK
    poll() enumerates the channels once and fetches each channel's newest 50 messages once. The
    text pass takes the messages newer than the cursor from that page (and pages from the cursor
    only when all 50 are newer), and the reaction pass reads the same page.

CLI
    ingest.py poll              # one sweep (text and reactions); JSON {"text":..., "reactions":...}
    ingest.py arm               # set every stream's last id to current latest (no back-processing)
    ingest.py list              # streams + channel_id + inbound flag (NO secrets)

SECRETS: never logs/prints the bot token or webhook URLs. Stdlib only.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import private_data
import inbound
import bot_credential
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
_PAGE = 50
_REACTOR_PAGE = 100
_STATE_DIR = None


def state_dir():
    return _STATE_DIR or str(private_data.config_root()/"state")


def prepare_state():
    private_data.check_destination(state_dir())
    os.makedirs(state_dir(), exist_ok=True)


def registry_path():
    return str(private_data.registry_path())


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
    """The bot token (reader.bot_token_ref via the credential vault, or inline reader.bot_token).
    Raises bot_credential.BotTokenUnavailable when a source is set but cannot produce a value."""
    return bot_credential.resolve(reg)


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
    the owner and MUST NOT fall back to None when it is unset (see poll): an unresolvable owner
    has to close this gate, not open it."""
    a = m.get("author") or {}
    if a.get("bot", False) or m.get("webhook_id"):
        return False
    if owner is not None and str(a.get("id") or "") != str(owner):
        return False
    return True


def _last_file(channel_id):
    """The cursor, keyed on the channel id. See the STATE note in the module docstring."""
    return os.path.join(state_dir(), "%s.last" % channel_id)


_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")


def _key(stream, channel_id):
    """Filename-safe state key: the stream name when it is one, else the channel id.

    Registered streams keep their historical file names (mail.reactions.seen). A discovered
    channel is named by whatever the human typed, which is not a filename, so it falls back to
    its id."""
    return stream if stream and _SAFE_KEY.match(stream) else str(channel_id)


def _streams(reg):
    """Registered, pollable streams: {name: channel_id}. The registry half of channels()."""
    out = {}
    excluded = _opted_out(reg)
    for name, s in (reg.get("streams") or {}).items():
        if s.get("channel_id") and str(s["channel_id"]) not in excluded:
            out[name] = s["channel_id"]
    return out


def _opted_out(reg):
    """Channel ids the owner has explicitly excluded, so discovery cannot add them back."""
    return {str(s["channel_id"]) for s in (reg.get("streams") or {}).values()
            if s.get("channel_id") and (not s.get("inbound", True) or not s.get("listen", True))}


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


def _dm_cache_file():
    return os.path.join(state_dir(), "owner-dm.channel")


def owner_dm_channel(reg, token):
    """The DM channel with the operator, opened if it does not exist yet, or None.

    A bot's own DM is a natural place to type at it, and the backdrop bot accepted commands there
    before the readers were merged. Returns None rather than raising: a DM being unreachable must
    cost the DM, not the guild sweep. A DM channel's id never changes, so it is opened once and
    cached per owner; only a missing or different-owner cache opens it again."""
    uid = str((reg.get("big_brother") or {}).get("user_id") or "")
    if not uid:
        return None
    try:
        with open(_dm_cache_file(), encoding="utf-8") as f:
            cached_uid, _, cached = f.read().strip().partition(":")
        if cached_uid == uid and cached.isdigit():
            return cached
    except OSError:
        pass
    try:
        req = urllib.request.Request(
            "%s/users/@me/channels" % _API, method="POST",
            data=json.dumps({"recipient_id": uid}).encode("utf-8"),
            headers={"Authorization": "Bot %s" % token, "User-Agent": _UA,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            channel = str(json.loads(r.read().decode("utf-8"))["id"])
    except Exception:
        return None
    try:
        prepare_state()
        with private_data.open_for_write(_dm_cache_file(), "w", encoding="utf-8") as f:
            f.write("%s:%s" % (uid, channel))
    except (OSError, ValueError):
        pass  # an unwritable cache costs one request per tick, never the DM itself
    return channel


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
        if dm and dm not in seen and dm not in _opted_out(reg):
            out.append(("dm", dm))
    return out


def _reaction_baseline_file(channel_id):
    return Path(state_dir()) / ("%s.reaction-baseline.json" % channel_id)


def _reaction_baseline(channel_id, owner):
    baseline = inbound._read(_reaction_baseline_file(channel_id))
    if baseline is not None:
        if (baseline.get("channel_id") != str(channel_id)
                or baseline.get("owner_id") != str(owner)
                or not isinstance(baseline.get("reaction_keys"), list)
                or not all(isinstance(key, str) for key in baseline["reaction_keys"])
                or not str(baseline.get("last_message_id", "")).isdigit()):
            raise ValueError("invalid channel reaction baseline")
    return baseline


def _admit_channel(channel_id, token, owner):
    """Persist the first reaction snapshot before the text cursor can admit a channel."""
    if not owner:
        raise RuntimeError("channel admission requires an explicit owner")
    path = _reaction_baseline_file(channel_id)
    with private_data.file_lock(str(path) + ".lock"):
        baseline = _reaction_baseline(channel_id, owner)
        if baseline is None:
            messages = _fetch(channel_id, token, limit=50)
            _, keys = reaction_events(channel_id, token, owner, messages, strict=True)
            baseline = {"schema_version": 1, "channel_id": str(channel_id),
                        "owner_id": str(owner), "reaction_keys": sorted(keys),
                        "last_message_id": str(messages[0]["id"]) if messages else "0"}
            inbound._write(path, baseline)
    return baseline


def poll_stream(stream, channel_id, token, owner=None, log=None, page=None):
    """Return list of new user-reply message dicts (oldest first); stage them; advance the cursor.

    THE INVARIANT: whatever this advances the cursor past is either staged as durable inbound work
    or was never the owner's to begin with. There is no third outcome where a message is consumed
    and dropped. A reader that skips what it does not recognise, and advances anyway, makes the
    message disappear with no error and no record; that is the bug this bus was reorganised
    around, and it is why staging and the cursor write live in one function.

    `page` is the channel's newest messages (newest first) already read this tick; the new ones are
    taken from it unless every message on a full page is newer than the cursor, in which case the
    read continues from the cursor as it does without a page."""
    prepare_state()
    lf = _last_file(channel_id)
    after = None
    if os.path.exists(lf):
        with open(lf) as f:
            after = f.read().strip()
        baseline = _reaction_baseline(channel_id, owner)
        valid = after.isascii() and after.isdigit()
        if not valid and baseline is None:
            raise ValueError("channel cursor is invalid and has no durable admission baseline")
        if baseline is not None and (not valid or int(after) < int(baseline["last_message_id"])):
            after = None
    if after is None:
        # A failed first write may leave an empty or partial cursor. Never replay before admission.
        baseline = _admit_channel(channel_id, token, owner)
        with private_data.open_for_write(lf, "w") as f:
            f.write(baseline["last_message_id"])
            f.flush()
            os.fsync(f.fileno())
        return []
    msgs = None
    if page is not None:
        msgs = [m for m in page if int(m["id"]) > int(after)]
        if len(msgs) >= _PAGE and len(msgs) == len(page):
            msgs = None                   # possibly more between the cursor and this page
    if msgs is None:
        msgs = _fetch(channel_id, token, after=after)
    if not msgs:
        return []
    if log and len(msgs) >= _PAGE:
        # One page, no pagination loop, and the cursor advances to the newest of this page. A full
        # page means the read may be partial; say so instead of letting it look complete.
        log("ingest: %s -> read a full page of %d message(s); this poll may not have covered the "
            "whole backlog" % (stream, len(msgs)))
    users = [m for m in reversed(msgs) if _is_user(m, owner)]  # oldest first
    # Every message read is staged, including ones a command handler will claim a moment later;
    # the tick decides separately what to forward to the judgment chain.
    for message in users:
        inbound.stage(stream, channel_id, message.get("id"), "text", message, state_dir())
    with private_data.open_for_write(lf, "w") as f:
        f.write(msgs[0]["id"])
        f.flush()
        os.fsync(f.fileno())
    return users


def format_messages(msgs):
    """The text rendering of a batch that dispatch judges."""
    out = []
    for m in msgs:
        out.append("[%s]\n" % m.get("timestamp", ""))
        if m.get("content"):
            out.append(m["content"] + "\n")
        for att in m.get("attachments", []):
            out.append("<attachment: %s %s>\n" % (att.get("filename"), att.get("url")))
        out.append("---\n")
    return "".join(out)


# ---------------------------------------------------------------- reactions (emoji replies)
# The user often answers a pushed alert by REACTING with an emoji instead of typing. A reaction
# lands on the webhook/bot alert itself (which poll_stream deliberately skips) and creates NO new
# message (so the `after` cursor never sees it). Reactions therefore get their own path: scan
# recent messages, read each message's `reactions`, confirm the OWNER reacted (not the bot),
# dedup against a per-stream seen-set, and stage a durable reaction record dispatch can judge like
# any other reply. Reading reactions over REST does NOT need the Message Content Intent.
#
# Extra STATE (the Agent Center state dir)
#     <stream>.reactions.seen   -- {"keys": processed "msgid:emoji:userid" keys, "counts":
#                                  {"msgid:emoji": reaction count at the last complete reactor
#                                  read}}, both bounded to the recent window. A reaction whose
#                                  count has not changed is not read again. (An older file holding
#                                  a bare key list still loads; its counts start empty.)

def owner_id(reg):
    return str((reg.get("big_brother") or {}).get("user_id") or "").strip() or None


def _emoji_ref(emoji):
    """(display, api_ref): unicode -> (char, %-quoted char); custom -> (:name:, name:id)."""
    name = emoji.get("name") or ""
    eid = emoji.get("id")
    if eid:
        return (":%s:" % name, "%s:%s" % (name, eid))
    return (name, urllib.parse.quote(name))


def _reactors(channel_id, msg_id, api_ref, token, limit=_REACTOR_PAGE, strict=False):
    """The reactor page, or None when it could not be read (strict raises instead)."""
    try:
        users = _get("%s/channels/%s/messages/%s/reactions/%s?limit=%d"
                     % (_API, channel_id, msg_id, api_ref, limit), token)
    except Exception:
        if strict:
            raise
        return None
    if len(users) >= limit:
        if strict:
            raise RuntimeError("channel admission requires a complete reactor snapshot")
        # One page only. The caller scans this list for the owner; an owner who reacted but fell
        # outside the page is indistinguishable here from an owner who did not react at all.
        print("ingest: reaction %s on message %s returned a full page of %d reactor(s); reactors "
              "beyond the first %d were not read" % (api_ref, msg_id, len(users), limit),
              file=sys.stderr)
    return users


def _seen_file(stream):
    return os.path.join(state_dir(), "%s.reactions.seen" % stream)


def _load_seen(stream):
    """(keys, counts); an unreadable file reads as empty, a bare list as keys without counts."""
    try:
        with open(_seen_file(stream), encoding="utf-8") as f:
            value = json.load(f)
        if isinstance(value, dict):
            return set(value.get("keys") or []), dict(value.get("counts") or {})
        return set(value), {}
    except Exception:
        return set(), {}


def _save_seen(stream, seen, counts=None):
    prepare_state()
    with private_data.open_for_write(_seen_file(stream), "w", encoding="utf-8") as f:
        json.dump({"keys": sorted(seen), "counts": dict(sorted((counts or {}).items()))}, f)


def _snippet(text, n=280):
    t = " ".join((text or "").split())
    return (t[:n] + "…") if len(t) > n else t


def reaction_events(channel_id, token, owner, msgs, strict=False, counts=None):
    """(events, all_owner_keys) for owner reactions on `msgs`. Pure of persisted state.
    Each event: {key, message_id, emoji, content, timestamp}. key = 'msgid:emoji:userid'.

    With `counts` ({"msgid:emoji": count} from the last tick), a reaction whose count is unchanged
    is skipped without reading its reactors (its keys are already in the seen set), and `counts`
    is updated in place with every count whose reactors were read completely."""
    if not owner:
        raise RuntimeError("reaction polling requires an explicit owner")
    owner = str(owner)
    events, keys = [], set()
    for m in msgs:
        mid = m["id"]
        for rx in (m.get("reactions") or []):
            emoji = rx.get("emoji") or {}
            if rx.get("count", 0) - (1 if rx.get("me") else 0) <= 0:
                continue  # only the bot itself reacted -> nothing from the user
            disp, api_ref = _emoji_ref(emoji)
            ekey = emoji.get("id") or emoji.get("name") or "?"
            slot = "%s:%s" % (mid, ekey)
            if counts is not None and counts.get(slot) == rx.get("count"):
                continue
            users = (_reactors(channel_id, mid, api_ref, token, strict=True) if strict
                     else _reactors(channel_id, mid, api_ref, token))
            if users is None:
                continue  # unread this tick; its count is not recorded, so the next tick retries
            if counts is not None and len(users) < _REACTOR_PAGE:
                counts[slot] = rx.get("count")   # a full page may be partial: read it again
            for u in users:
                uid = str(u.get("id") or "")
                if u.get("bot") or (owner and uid != owner):
                    continue
                key = "%s:%s:%s" % (mid, ekey, uid)
                keys.add(key)
                events.append({"key": key, "message_id": mid, "emoji": disp,
                               "content": _snippet(m.get("content")), "timestamp": m.get("timestamp", "")})
    return events, keys


def poll_reactions_stream(stream, channel_id, token, owner, limit=_PAGE, messages=None):
    """New owner reactions on recent messages -> staged durable records; return new events.

    `messages` is the page this tick already read for the channel (newest first)."""
    if not owner:
        raise RuntimeError("reaction polling requires an explicit owner")
    prepare_state()
    stream_name = stream
    stream = _key(stream, channel_id)
    msgs = messages if messages is not None else _fetch(channel_id, token, limit=limit)
    if not msgs:
        return []
    stored, stored_counts = _load_seen(stream)
    seen = set(stored)
    baseline = _reaction_baseline(channel_id, owner)
    if baseline is not None:
        seen.update(baseline["reaction_keys"])
    counts = dict(stored_counts)
    all_events, all_keys = reaction_events(channel_id, token, owner, msgs, counts=counts)
    window = {m["id"] for m in msgs}
    new = [e for e in all_events if e["key"] not in seen]
    # Stage durable work before writing the bounded seen-set.
    for event in new:
        inbound.stage(stream_name, channel_id, event["key"], "reaction", event, state_dir())
    keys = {k for k in (seen | all_keys) if k.split(":", 1)[0] in window}
    counts = {slot: n for slot, n in counts.items() if slot.split(":", 1)[0] in window}
    if keys != stored or counts != stored_counts:
        _save_seen(stream, keys, counts)
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


def poll(reg=None, token=None, log=None, deadline=None):
    """One sweep of every channel: {"text": {stream: n}, "reactions": {stream: n}}.

    Channels are enumerated once and each channel's newest page is read once; the text pass and
    the reaction pass both work from it. The reaction pass runs only for a channel whose text
    cursor exists, because admitting a channel (poll_stream) is what records the reaction
    baseline that keeps old reactions from being replayed. `deadline` (time.monotonic()) stops
    the sweep from starting another channel once it has passed; the rest wait for the next tick."""
    reg = reg if reg is not None else load_registry()
    token = token or bot_token(reg)
    if not token:
        raise RuntimeError("no bot token: set registry.reader.bot_token_ref (or reader.bot_token) in the Agent Center registry")
    # Fail CLOSED on an unresolvable owner. A reply can enqueue execution on this machine, so an
    # unset owner must stop the poll rather than quietly widen it to everyone who can post in the
    # channel. This mirrors the missing-token guard directly above.
    owner = owner_id(reg)
    if not owner:
        raise RuntimeError("no owner: set registry.big_brother.user_id; inbound replies are "
                           "owner-only because they can start real work")
    text, reactions = {}, {}
    for stream, ch in channels(reg, token, log=log):
        if deadline is not None and time.monotonic() > deadline:
            if log:
                log("ingest: poll budget spent; the remaining channels wait for the next tick")
            break
        try:
            page = _fetch(ch, token, limit=_PAGE)  # newest first
        except Exception as e:
            if log:
                log("ingest: %s poll error: %s" % (stream, type(e).__name__))
            continue
        try:
            users = poll_stream(stream, ch, token, owner, log=log, page=page)
            if users:
                text[stream] = len(users)
                if log:
                    log("ingest: %s -> %d new reply(ies)" % (stream, len(users)))
        except Exception as e:
            if log:
                log("ingest: %s poll error: %s" % (stream, type(e).__name__))
        if not os.path.exists(_last_file(ch)):
            continue
        try:
            new = poll_reactions_stream(stream, ch, token, owner, messages=page)
            if new:
                reactions[stream] = len(new)
                if log:
                    log("ingest: %s -> %d new reaction(s)" % (stream, len(new)))
        except Exception as e:
            if log:
                log("ingest: %s reaction poll error: %s" % (stream, type(e).__name__))
    return {"text": text, "reactions": reactions}


def arm_reactions(reg, token):
    """Save complete owner-reaction snapshots for the selected recent message window."""
    owner = owner_id(reg)
    if not owner:
        raise RuntimeError("reaction polling requires an explicit owner")
    n = 0
    for stream, ch in channels(reg, token):
        messages = _fetch(ch, token, limit=50)
        _, keys = reaction_events(ch, token, owner, messages, strict=True)
        _save_seen(_key(stream, ch), keys)
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(prog="ingest.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll")
    sub.add_parser("arm")
    sub.add_parser("list")
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
    if a.cmd == "arm":
        tok = bot_token(reg)
        prepare_state()
        n = 0
        try:
            # Complete reaction admission before advancing any text cursor.
            rn = arm_reactions(reg, tok)
            for stream, ch in channels(reg, tok):
                latest = _fetch(ch, tok, limit=1)
                if latest:
                    with private_data.open_for_write(_last_file(ch), "w") as f:
                        f.write(latest[0]["id"])
                        f.flush()
                        os.fsync(f.fileno())
                    n += 1
        except (OSError, ValueError, RuntimeError):
            print(json.dumps({"ok": False, "error": "channel arming did not complete",
                              "retry_required": True}))
            return 1
        print(json.dumps({"armed": n, "armed_reactions": rn}))
        return 0
    if a.cmd == "poll":
        res = poll(reg, log=lambda m: print(m, file=sys.stderr))
        print(json.dumps(res, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())


def pending_work():
    return inbound.pending(state_dir())


def format_reaction(event):
    return ("Owner reaction: %s\nMessage: %s\nTimestamp: %s\n" %
            (event["emoji"], event.get("content") or "", event.get("timestamp") or ""))
