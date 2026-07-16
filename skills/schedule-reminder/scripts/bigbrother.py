#!/usr/bin/env python3
"""schedule-reminder — native Big Brother DM sender (stdlib only).

The Agent Center digest (and any last-resort fallback) pushes to the operator's Discord DM, which is
the only channel that reliably reaches a phone. This used to shell out to an ad-hoc
`the legacy DM notifier script` (which needed the `requests` package and its own `config.json`).
This module folds that capability into the skill: it opens the bot->user DM and posts, using stdlib
`urllib` and the SAME registry every other Agent Center path reads.

Config (from the registry, discovery via relay.load_registry):
  reader.bot_token        the Discord bot token (canonical; same one ingest reads)
  big_brother.user_id     the recipient user id for the DM

Honors AGENT_CENTER_RELAY_DRYRUN (skip network, return True) so tests/CI never hit Discord.
Contract: send_dm(text) -> bool. Never raises for delivery errors.
"""
import json
import os
import sys
import urllib.request

_API = "https://discord.com/api/v10"
_MAX = 1990
# The Bot message-create endpoint WAF-403s a browser User-Agent (code 40333 "internal network
# error") when paired with a Bot token -- a browser never posts via a bot token, so it reads as
# abuse. The official Bot UA form is required for WRITES. (Reads/GET and webhook POSTs are fine with
# a browser UA, which is why ingest.py/relay.py get away with it.)
_UA = "DiscordBot (https://github.com/DaizeDong/schedule-reminder, 0.4.0)"


def _registry():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import relay  # sibling; reuse the one registry discovery (AGENT_CENTER_CONFIG / default)
    return relay.load_registry()


def _creds(reg):
    token = (reg.get("reader") or {}).get("bot_token")
    user_id = (reg.get("big_brother") or {}).get("user_id")
    return token, user_id


def _chunk(text, limit=_MAX):
    out, rem = [], text
    while rem:
        if len(rem) <= limit:
            out.append(rem)
            break
        cut = rem.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(rem[:cut])
        rem = rem[cut:].lstrip("\n")
    return out or [""]


def _post(url, token, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": "Bot %s" % token, "Content-Type": "application/json", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8") or "{}")


def send_dm(text):
    """Deliver `text` to the Big Brother DM. Returns True on success, False on failure/misconfig."""
    if os.environ.get("AGENT_CENTER_RELAY_DRYRUN"):
        sys.stdout.write("DRYRUN big-brother DM: %s\n" % (text or "")[:80])
        return True
    try:
        token, user_id = _creds(_registry())
        if not token or not user_id:
            sys.stderr.write("bigbrother: no token/user_id in registry (reader.bot_token / "
                             "big_brother.user_id)\n")
            return False
        st, body = _post("%s/users/@me/channels" % _API, token, {"recipient_id": str(user_id)})
        if st != 200 or "id" not in body:
            sys.stderr.write("bigbrother: open DM failed (status %s)\n" % st)
            return False
        chan = body["id"]
        for piece in _chunk(text or ""):
            st, _ = _post("%s/channels/%s/messages" % (_API, chan), token, {"content": piece})
            if st not in (200, 204):
                sys.stderr.write("bigbrother: post failed (status %s)\n" % st)
                return False
        return True
    except Exception as e:  # delivery failures are a return value, never an exception
        sys.stderr.write("bigbrother: %s\n" % type(e).__name__)
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python bigbrother.py <text>\n")
        raise SystemExit(2)
    raise SystemExit(0 if send_dm(sys.argv[1]) else 1)
