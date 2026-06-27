#!/usr/bin/env python3
"""schedule-reminder — Agent Center multi-stream relay (the single Discord egress for all skills).

WHY THIS EXISTS
    Before this, every skill shelled out to the Big Brother DM relay, so all alerts piled into one
    DM stream. The Agent Center model gives each message *type* its own channel + identity. This
    module is the single, frozen egress every downstream skill calls — so the transport (webhook vs
    bot vs anything else) can change forever without touching any skill.

REGISTRY (secret, never committed)
    Discovery order: env AGENT_CENTER_CONFIG, else ~/.agent-center/registry.json.
    Shape: {"streams": {"<name>": {"webhook": "...", "username": "..."}}, "big_brother": {...}}
    Each stream posts to its webhook; per-message `username` gives the stream its identity in Discord.

CONTRACT (frozen surface downstream skills depend on — subprocess, never import internals)
    relay(stream, content, username=None) -> bool          # True = delivered
    CLI:
      relay.py send   --stream NAME (--text T | --json '{"content":..,"username":..}')
      relay.py digest --text T            # aggregated daily summary -> Big Brother DM
      relay.py list                       # show configured streams (NO secrets)
      relay.py health                     # registry present? streams sane? (NO network, NO secrets)

ROBUSTNESS
    Unknown stream / missing registry  -> fall back to Big Brother DM (via notify.py) so a message is
    never silently lost; a one-line warning goes to stderr (never the webhook URL).

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

# Discord 403s the default urllib UA — a real User-Agent is mandatory.
_UA = "AgentCenter-Relay/1.0 (+https://discord.com)"
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
    """POST a webhook payload. Honors AGENT_CENTER_RELAY_DRYRUN (no network) for tests/CI."""
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


def _big_brother(text: str) -> bool:
    """Fallback / digest channel: deliver via the Big Brother DM relay (notify.py)."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import notify  # noqa: E402  (local sibling module)
        return bool(notify.notify(text))
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


def digest(content: str) -> bool:
    """Deliver the aggregated daily summary via Big Brother DM (registry.big_brother)."""
    return _big_brother(content)


def _cmd_list() -> int:
    reg = load_registry()
    streams = reg.get("streams") or {}
    if not streams:
        print(json.dumps({"ok": False, "registry": registry_path(), "streams": []}))
        return 1
    # NEVER print webhook URLs — only safe metadata.
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
    p_send = sub.add_parser("send", help="send to a stream")
    p_send.add_argument("--stream", required=True)
    g = p_send.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--json", dest="json_payload", help='{"content":..,"username":..}')
    p_send.add_argument("--username", default=None)
    p_dig = sub.add_parser("digest", help="send aggregated daily summary to Big Brother")
    p_dig.add_argument("--text", required=True)
    sub.add_parser("list", help="list configured streams (no secrets)")
    sub.add_parser("health", help="check registry health (no network, no secrets)")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        return _cmd_list()
    if args.cmd == "health":
        return _cmd_health()
    if args.cmd == "digest":
        return 0 if digest(args.text) else 1
    if args.cmd == "send":
        if args.json_payload:
            try:
                obj = json.loads(args.json_payload)
            except Exception as e:
                sys.stderr.write("relay: bad --json (%s)\n" % e)
                return 2
            content = obj.get("content", "")
            username = obj.get("username") or args.username
        else:
            content, username = args.text, args.username
        return 0 if relay(args.stream, content, username) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
