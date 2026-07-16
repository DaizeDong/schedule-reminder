#!/usr/bin/env python3
"""schedule-reminder — Agent Center multi-stream INGEST (the single Discord ingress).

The inbound mirror of relay.py. relay.py POSTs skill output to each stream's webhook (out);
this module GETs each stream's channel for new USER replies (in). Webhooks are send-only, but a
bot with read access can pull channel history over REST (no privileged Message Content Intent).

REGISTRY (secret; the Agent Center registry or env AGENT_CENTER_CONFIG)
    streams.<name>.channel_id   -- required to poll a stream (absent -> stream skipped)
    streams.<name>.inbound      -- optional; set false to opt a stream out of ingest
    reader.bot_token            -- optional; else falls back to the legacy notifier config

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
import urllib.parse
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

_UA = "AgentCenter-Ingest/1.0 (+https://discord.com)"
_API = "https://discord.com/api/v10"
_DEFAULT_REGISTRY = os.path.join(os.path.expanduser("~"), ".agent-center", "registry.json")
_STATE_DIR = os.path.join(os.path.expanduser("~"), ".agent-center", "state")
_FALLBACK_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".claude", "discord_relay", "config.json")


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
    tok = (reg.get("reader") or {}).get("bot_token")
    if tok:
        return tok
    try:
        with open(_FALLBACK_TOKEN_FILE, encoding="utf-8") as fh:
            return json.load(fh).get("bot_token")
    except Exception:
        return None


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bot %s" % token, "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch(channel_id, token, after=None, limit=50):
    q = {"limit": limit}
    if after:
        q["after"] = after
    return _get("%s/channels/%s/messages?%s" % (_API, channel_id, urllib.parse.urlencode(q)), token)


def _is_user(m):
    """A genuine human reply: not a bot, not a webhook post (our own alerts/confirmations)."""
    a = m.get("author") or {}
    return not a.get("bot", False) and not m.get("webhook_id")


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


def poll_stream(stream, channel_id, token):
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
    users = [m for m in reversed(msgs) if _is_user(m)]  # oldest first
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
        raise RuntimeError("no bot token (registry.reader.bot_token or discord_relay/config.json)")
    result = {}
    for stream, ch in _streams(reg).items():
        try:
            users = poll_stream(stream, ch, token)
            if users:
                result[stream] = len(users)
                if log:
                    log("ingest: %s -> %d new reply(ies)" % (stream, len(users)))
        except Exception as e:
            if log:
                log("ingest: %s poll error: %s" % (stream, type(e).__name__))
    return result


def main():
    ap = argparse.ArgumentParser(prog="ingest.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll")
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
        print(json.dumps({"armed": n}))
        return 0
    if a.cmd == "poll":
        res = poll_all(reg, log=lambda m: print(m, file=sys.stderr))
        print(json.dumps(res, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
