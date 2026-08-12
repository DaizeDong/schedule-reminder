#!/usr/bin/env python3
"""schedule-reminder — Agent Center multi-stream relay (the single Discord egress for all skills).

WHY THIS EXISTS
    Before this, every skill shelled out to the Big Brother DM relay, so all alerts piled into one
    DM stream. The Agent Center model gives each message *type* its own channel + identity. This
    module is the single, frozen egress every downstream skill calls — so the transport (webhook vs
    bot vs anything else) can change forever without touching any skill.

REGISTRY (secret, never committed)
    Discovery order: env AGENT_CENTER_CONFIG, else the registry file in the Agent Center config dir.
    Shape: {"streams": {"<name>": {"webhook": "...", "username": "..."}}, "big_brother": {...}}
    Each stream posts to its webhook; per-message `username` gives the stream its identity in Discord.

CONTRACT (frozen surface downstream skills depend on — subprocess, never import internals)
    relay(stream, content, username=None) -> bool          # True = delivered
    send(content, stream=..., channel_id=..., files=[...]) -> bool
    CLI:
      relay.py send   --stream NAME (--text T | --json '{"content":..,"username":..}')
      relay.py send   --channel-id ID --text T [--file PATH ...]
      relay.py digest --text T            # aggregated daily summary -> Big Brother DM
      relay.py list                       # show configured streams (NO secrets)
      relay.py health                     # registry present? streams sane? (NO network, NO secrets)

TWO TRANSPORTS, ONE EGRESS
    Webhook is the default: it carries the per-stream identity (username + avatar) that makes the
    Agent Center readable at a glance, and it needs no bot permissions. But a webhook is BOUND to
    the channel it was created for and cannot carry a file, so two jobs are impossible on it:
    answering in whichever channel a command was typed in, and posting an image. Those go over the
    bot token instead (registry.reader.bot_token, the same one ingest reads with).

    Transport is chosen from what the caller asks for, never configured:
        files given, or channel_id given   -> bot
        otherwise                          -> the stream's webhook
    This exists so a caller never has to know which one it is on. Before it, every job the webhook
    could not do grew its own hand written Discord client (three of them: the backdrop bot, the
    guestbook moderator, the promotion sender), each with its own UA, retry and 403 handling. The
    point of a single egress is that adding a capability here retires a fork out there.

ROBUSTNESS
    Unknown stream / missing registry  -> fall back to Big Brother DM (via notify.py) so a message is
    never silently lost; a one-line warning goes to stderr (never the webhook URL).
    A bot send has NO such fallback and returns False: it is addressed at one specific channel, and
    silently rerouting "the answer to what you just typed in #here" into a DM is worse than a
    visible failure the caller can report in place.

SECRETS
    Webhook URLs live ONLY in the registry file. This module never logs, prints, or echoes them.

GOTCHA (encoded here so it is never relearned)
    Discord/Cloudflare returns HTTP 403 for the default python-urllib User-Agent. A real UA header
    is mandatory; see _UA below.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

# Output is always UTF-8 regardless of host console code page (Windows GBK consoles 403 emoji otherwise).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Discord 403s the default urllib UA, a real User-Agent is mandatory.
_UA = "AgentCenter-Relay/1.0 (+https://discord.com)"
_API = "https://discord.com/api/v10"
_DEFAULT_REGISTRY = os.path.join(os.path.expanduser("~"), ".agent-center", "registry.json")


def registry_path() -> str:
    return os.environ.get("AGENT_CENTER_CONFIG") or _DEFAULT_REGISTRY


def load_registry() -> dict:
    """Return the registry dict, or {} if absent/unreadable (caller falls back to Big Brother)."""
    p = registry_path()
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # malformed registry must not crash a skill's alert path
        sys.stderr.write("relay: registry unreadable (%s)\n" % e)
        return {}


def _post_webhook(url: str, payload: dict) -> bool:
    """POST a webhook payload. Honors AGENT_CENTER_RELAY_DRYRUN (no network) for tests/CI.

    Suppress Discord's auto-generated link-preview embeds by default (flags=4 = SUPPRESS_EMBEDS):
    this relay is content-only by design, and the unsolicited link cards are pure noise. A caller
    that genuinely wants embeds can pass flags=0 in a --json payload to opt back in.
    """
    payload.setdefault("flags", 4)
    if os.environ.get("AGENT_CENTER_RELAY_DRYRUN"):
        sys.stdout.write("DRYRUN webhook <%s> %s\n" % (payload.get("username", "?"),
                                                       (payload.get("content", "") or "")[:80]))
        return True
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status in (200, 204)
    except Exception as e:
        sys.stderr.write("relay: webhook POST failed (%s)\n" % e)
        return False


def bot_token(reg: dict) -> str | None:
    """The bot token, canonical source. Same key ingest.py reads, deliberately: one credential for
    the whole bus means one place to rotate it."""
    return (reg.get("reader") or {}).get("bot_token") or None


def _post_bot(channel_id: str, content: str, files: list | None, token: str) -> bool:
    """POST as the bot to one channel, with optional file attachments.

    Multipart is assembled by hand rather than pulled from a library because this runs from
    scheduled tasks under whatever python is on PATH, and the whole Agent Center is stdlib only for
    that reason. Content is NOT silently truncated: a caller that overruns Discord's 2000 character
    limit gets a visible failure, because a summary that quietly loses its tail reads as complete.
    """
    if os.environ.get("AGENT_CENTER_RELAY_DRYRUN"):
        sys.stdout.write("DRYRUN bot <%s> %s%s\n" % (
            channel_id, (content or "")[:80],
            (" +%d file(s)" % len(files)) if files else ""))
        return True
    url = "%s/channels/%s/messages" % (_API, channel_id)
    headers = {"Authorization": "Bot %s" % token, "User-Agent": _UA}
    if not files:
        req = urllib.request.Request(
            url, data=json.dumps({"content": content or ""}).encode("utf-8"), method="POST",
            headers={**headers, "Content-Type": "application/json"})
    else:
        paths = [str(f) for f in files]
        boundary = "----agentcenter" + os.urandom(8).hex()
        payload = {"content": content or "",
                   "attachments": [{"id": i, "filename": os.path.basename(p)}
                                   for i, p in enumerate(paths)]}
        parts: list[bytes] = []

        def field(name, value, filename=None, ctype=None):
            head = '--%s\r\nContent-Disposition: form-data; name="%s"' % (boundary, name)
            if filename:
                head += '; filename="%s"' % filename
            head += "\r\n"
            if ctype:
                head += "Content-Type: %s\r\n" % ctype
            parts.append(head.encode("utf-8") + b"\r\n" + value + b"\r\n")

        field("payload_json", json.dumps(payload).encode("utf-8"), ctype="application/json")
        for i, p in enumerate(paths):
            with open(p, "rb") as fh:
                field("files[%d]" % i, fh.read(), os.path.basename(p), "application/octet-stream")
        parts.append(("--%s--\r\n" % boundary).encode())
        req = urllib.request.Request(
            url, data=b"".join(parts), method="POST",
            headers={**headers, "Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status in (200, 204)
    except Exception as e:
        sys.stderr.write("relay: bot POST failed (%s)\n" % e)
        return False


def _big_brother(text: str) -> bool:
    """Fallback / digest target: the operator's Big Brother DM (registry.big_brother), delivered by
    the native `bigbrother` sender. This is the phone-reaching channel — the digest and any
    unknown-stream fallback land here, as documented in `reference/agent-center.md`."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import bigbrother  # noqa: E402  (local sibling module; stdlib DM sender)
        return bool(bigbrother.send_dm(text))
    except Exception as e:
        sys.stderr.write("relay: big-brother fallback failed (%s)\n" % e)
        return False


def relay(stream: str, content: str, username: str | None = None) -> bool:
    """Deliver `content` to the named Agent Center stream. Returns True on success.

    Resolution: registry.streams[stream].webhook (per-stream identity via `username`).
    Fallback: if the stream is unknown or no registry exists, deliver to Big Brother DM so the
    message is never lost (prefixed with the stream name for context).
    """
    reg = load_registry()
    s = (reg.get("streams") or {}).get(stream)
    if not s or not s.get("webhook"):
        sys.stderr.write("relay: stream %r not configured; using Big Brother fallback\n" % stream)
        return _big_brother("[%s] %s" % (stream, content))
    payload = {"content": content, "username": username or s.get("username") or stream}
    return _post_webhook(s["webhook"], payload)


def send(content: str, stream: str | None = None, channel_id: str | None = None,
         files: list | None = None, username: str | None = None) -> bool:
    """Deliver to a stream, to an explicit channel, or both, choosing the transport (see module doc).

    `stream` alone behaves exactly like relay() and keeps the per-stream webhook identity.
    `channel_id` (or any `files`) switches to the bot, because a webhook can do neither.
    Given both, `channel_id` wins for routing and `stream` is used only to resolve a channel when
    the caller passed a name instead of an id.
    """
    reg = load_registry()
    s = (reg.get("streams") or {}).get(stream) if stream else None
    chan = channel_id or (s or {}).get("channel_id")
    if not files and not channel_id:
        return relay(stream, content, username)          # the frozen path, unchanged
    if not chan:
        sys.stderr.write("relay: no channel for stream %r; cannot use the bot transport\n" % stream)
        return False
    token = bot_token(reg)
    if not token:
        sys.stderr.write("relay: no reader.bot_token in the registry; cannot use the bot transport\n")
        return False
    return _post_bot(str(chan), content, files, token)


def digest(content: str) -> bool:
    """Deliver the aggregated daily summary via Big Brother DM (registry.big_brother)."""
    return _big_brother(content)


def _cmd_list() -> int:
    reg = load_registry()
    streams = reg.get("streams") or {}
    if not streams:
        print(json.dumps({"ok": False, "registry": registry_path(), "streams": []}))
        return 1
    # NEVER print webhook URLs, only safe metadata.
    out = {name: {k: v for k, v in s.items() if k != "webhook"} for name, s in streams.items()}
    print(json.dumps({"ok": True, "registry": registry_path(),
                      "guild_id": reg.get("guild_id"), "streams": out}, ensure_ascii=False, indent=2))
    return 0


def _cmd_health() -> int:
    reg = load_registry()
    streams = reg.get("streams") or {}
    problems = []
    if not reg:
        problems.append("registry missing at %s" % registry_path())
    for name, s in streams.items():
        if not s.get("webhook", "").startswith("https://"):
            problems.append("stream %s: missing/invalid webhook" % name)
    ok = not problems
    print(json.dumps({"ok": ok, "registry": registry_path(),
                      "stream_count": len(streams), "problems": problems}, ensure_ascii=False))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="relay.py", description="Agent Center multi-stream Discord relay")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_send = sub.add_parser("send", help="send to a stream or to one channel")
    p_send.add_argument("--stream", default=None)
    p_send.add_argument("--channel-id", dest="channel_id", default=None,
                        help="post as the bot to this channel (answers where the user is looking)")
    p_send.add_argument("--file", dest="files", action="append", default=None,
                        help="attach a file (repeatable); forces the bot transport")
    g = p_send.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    # Windows PowerShell 5.1 mangles a non-ASCII argv (em/CJK -> mojibake) when it invokes python.exe,
    # so a PS caller must base64-encode the UTF-8 bytes and pass them here instead of --text/--json.
    g.add_argument("--text-b64", dest="text_b64", help="base64 of UTF-8 text (PowerShell-safe)")
    g.add_argument("--json", dest="json_payload", help='{"content":..,"username":..}')
    g.add_argument("--json-b64", dest="json_b64", help="base64 of UTF-8 JSON (PowerShell-safe)")
    p_send.add_argument("--username", default=None)
    p_dig = sub.add_parser("digest", help="send aggregated daily summary to Big Brother")
    gd = p_dig.add_mutually_exclusive_group(required=True)
    gd.add_argument("--text")
    gd.add_argument("--text-b64", dest="text_b64", help="base64 of UTF-8 text (PowerShell-safe)")
    sub.add_parser("list", help="list configured streams (no secrets)")
    sub.add_parser("health", help="check registry health (no network, no secrets)")
    args = ap.parse_args(argv)

    def _b64(s):
        import base64
        return base64.b64decode(s).decode("utf-8")

    if args.cmd == "list":
        return _cmd_list()
    if args.cmd == "health":
        return _cmd_health()
    if args.cmd == "digest":
        text = _b64(args.text_b64) if getattr(args, "text_b64", None) else args.text
        return 0 if digest(text) else 1
    if args.cmd == "send":
        if not args.stream and not args.channel_id:
            sys.stderr.write("relay: send needs --stream or --channel-id\n")
            return 2
        payload = None
        if args.json_b64:
            payload = _b64(args.json_b64)
        elif args.json_payload:
            payload = args.json_payload
        if payload is not None:
            try:
                obj = json.loads(payload)
            except Exception as e:
                sys.stderr.write("relay: bad --json (%s)\n" % e)
                return 2
            content = obj.get("content", "")
            username = obj.get("username") or args.username
        elif args.text_b64:
            content, username = _b64(args.text_b64), args.username
        else:
            content, username = args.text, args.username
        return 0 if send(content, stream=args.stream, channel_id=args.channel_id,
                         files=args.files, username=username) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
