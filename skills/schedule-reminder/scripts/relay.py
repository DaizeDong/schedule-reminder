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

# Discord rejects a single message whose `content` exceeds 2000 characters with HTTP 400,
# and the whole message is then lost. _CHUNK_BUDGET leaves room for the "(n/m)" marker that
# every part of a split message carries.
_DISCORD_LIMIT = 2000
_CHUNK_BUDGET = 1900


def split_for_discord(text: str, budget: int = _CHUNK_BUDGET) -> list[str]:
    """Split an over-long body into deliverable parts, each marked "(n/m)".

    Why this exists. The original design refused to truncate, on the correct grounds that a
    summary which quietly loses its tail reads as complete. But refusing to truncate was
    implemented as refusing to adapt at all, so an over-long body produced HTTP 400 and the
    caller lost the ENTIRE message rather than its tail. Losing everything is strictly worse
    than losing the end, and it fails in the worst possible direction: an alert body that
    enumerates failures grows with the number of failures, so the more there is to report,
    the less likely the report is to arrive. Measured on a monitoring digest: three
    consecutive HTTP 400s at 2066, 2072 and 2006 characters, all just over the wall.

    So: never silently drop anything, and never drop everything. Split on line boundaries
    where possible, hard-split only a single line that is itself over budget, and mark every
    part so a reader can see that more is coming. A caller that wants the old all-or-nothing
    behaviour can check len(text) itself before calling.
    """
    if len(text) <= _DISCORD_LIMIT:
        return [text]

    pieces: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > budget:
            # A single line longer than the budget: emit what is left of the current piece,
            # then hard-split the line. Nothing is dropped, the break is just not on a newline.
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(line[:budget])
            line = line[budget:]
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= budget:
            cur += "\n" + line
        else:
            pieces.append(cur)
            cur = line
    if cur:
        pieces.append(cur)

    total = len(pieces)
    out = ["(%d/%d)\n%s" % (i + 1, total, p) for i, p in enumerate(pieces)]
    # The marker must never be what pushes a part back over the wall.
    assert all(len(p) <= _DISCORD_LIMIT for p in out), "chunking produced an over-limit part"
    return out


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
    that reason. Content is never silently truncated: an over-long body is split by
    split_for_discord into marked parts so that nothing is lost and the split is visible. It used
    to be neither truncated nor split, which meant an over-long body was lost in full; see that
    function for why that is the worse of the two failures.
    """
    parts = split_for_discord(content or "")
    if len(parts) > 1:
        # Attachments ride the first part; the rest carry text only. Sending the files with
        # every part would upload them N times.
        ok = _post_bot(channel_id, parts[0], files, token)
        for part in parts[1:]:
            if not _post_bot(channel_id, part, None, token):
                ok = False
        sys.stderr.write("relay: body was %d chars, delivered as %d parts\n"
                         % (len(content or ""), len(parts)))
        return ok
    content = parts[0]
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
        # This is the LAST channel: whatever could not be delivered anywhere else arrives here.
        # An over-long body must not die on the one path that exists to catch the others.
        parts = split_for_discord(text or "")
        ok = True
        for part in parts:
            if not bigbrother.send_dm(part):
                ok = False
        return ok
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
    name = username or s.get("username") or stream
    parts = split_for_discord(content or "")
    ok = True
    for part in parts:
        # Every part must land. Returning True after a partial delivery would report a
        # message as sent while its tail is missing, which is the failure this whole
        # function exists to avoid.
        if not _post_webhook(s["webhook"], {"content": part, "username": name}):
            ok = False
    if len(parts) > 1:
        sys.stderr.write("relay: body was %d chars, delivered as %d parts\n"
                         % (len(content or ""), len(parts)))
    return ok


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
