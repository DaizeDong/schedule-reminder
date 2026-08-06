#!/usr/bin/env python3
"""schedule-reminder — Agent Center multi-stream INGEST (the single Discord ingress).

The inbound mirror of relay.py. relay.py POSTs skill output to each stream's webhook (out);
this module GETs each stream's channel for new USER replies (in). Webhooks are send-only, but a
bot with read access can pull channel history over REST (no privileged Message Content Intent).

REGISTRY (secret; env AGENT_CENTER_CONFIG, else the registry file in the Agent Center config dir)
    streams.<name>.channel_id   -- required to poll a stream (absent -> stream skipped)
    streams.<name>.inbound      -- optional; set false to opt a stream out of ingest
    reader.bot_token            -- the Discord bot token (canonical; same one relay/bigbrother use)

STATE (the Agent Center state dir)
    <stream>.last   -- last processed message id per stream (advances every poll)
    <stream>.inbox  -- newest batch of user replies for that stream (consumed by dispatch)

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


def _last_file(stream):
    return os.path.join(_STATE_DIR, "%s.last" % stream)


def _inbox_file(stream):
    return os.path.join(_STATE_DIR, "%s.inbox" % stream)


def _streams(reg):
    out = {}
    for name, s in (reg.get("streams") or {}).items():
        if s.get("channel_id") and s.get("inbound", True):
            out[name] = s["channel_id"]
    return out


def poll_stream(stream, channel_id, token, owner=None):
    """Return list of new user-reply message dicts (oldest first); advance last id; write inbox."""
    os.makedirs(_STATE_DIR, exist_ok=True)
    lf = _last_file(stream)
    if not os.path.exists(lf):
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
        with open(_inbox_file(stream), "w", encoding="utf-8") as f:
            for m in users:
                f.write("[%s]\n" % m.get("timestamp", ""))
                if m.get("content"):
                    f.write(m["content"] + "\n")
                for att in m.get("attachments", []):
                    f.write("<attachment: %s %s>\n" % (att.get("filename"), att.get("url")))
                f.write("---\n")
    return users


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
    global LAST_POLL_IDS
    LAST_POLL_IDS = {}
    for stream, ch in _streams(reg).items():
        try:
            users = poll_stream(stream, ch, token, owner)
            if users:
                result[stream] = len(users)
                LAST_POLL_IDS[stream] = (ch, [m["id"] for m in users if m.get("id")])
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
    for stream, ch in _streams(reg).items():
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
    for stream, ch in _streams(reg).items():
        try:
            _, keys = reaction_events(ch, token, owner, _fetch(ch, token, limit=50))
            _save_seen(stream, keys)
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
        print(json.dumps({"streams": streams}, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "inbox":
        p = _inbox_file(a.stream)
        print(open(p, encoding="utf-8").read() if os.path.exists(p) else "")
        return 0
    if a.cmd == "arm":
        tok = bot_token(reg)
        os.makedirs(_STATE_DIR, exist_ok=True)
        n = 0
        for stream, ch in _streams(reg).items():
            latest = _fetch(ch, tok, limit=1)
            if latest:
                with open(_last_file(stream), "w") as f:
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
