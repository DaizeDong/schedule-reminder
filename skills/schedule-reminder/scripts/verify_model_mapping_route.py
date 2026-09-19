#!/usr/bin/env python3
"""Prove each model-mapping notification really travels the production path into its own channel.

A probe posted by the admin tool only proves the channel exists and the bot can write to it. It says
nothing about the sender, and the sender is the half that rots: cc-model-refresh kept a private DM
client for months after the Agent Center existed, and codexg-model-refresh pointed at a relay path
that is not on this machine at all, so `if not os.path.isfile(relay): return` made every one of its
announcements a silent no-op. Both of those states look identical to a channel probe: green channel,
empty channel. An empty notification channel is an unmigrated sender, not a quiet week.

So this drives the ACTUAL production entry point of each sender and then reads the channel back to
find the message that entry point produced.

Stubbed, and only for the apply-map entry: the .env writer and the proxy reloader. Writing config and
hot-reloading a live proxy are side effects on running infrastructure and neither is on the
notification path. The diff, the message text, the notifier and the transport are the real ones.

Each route is driven in its own process. The senders are sibling jobs with same-named modules
(`config`, `apply`, `state`), so importing two of them into one interpreter would silently hand the
second one the first one's config.

Exit 0 only when every checked route's message is visible in the channel its registry entry binds it
to, inside the expected category. Anything else exits 1: "could not check" and "checked and clean"
must never print the same thing.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import relay  # noqa: E402

# The fleet-wide notification language rule. Imported from its installed location rather than
# vendored, for the same reason the senders are: a copy here would keep agreeing with itself while
# the real rule moved.
#
# The explicit environment dependency supports packaged deployments and synthetic tests.
# Production keeps its installed-path default. Missing policy remains a hard failure; arrival
# without a language check cannot be reported as a checked route.
_RULE_PATH = os.environ.get("SCHEDULE_NOTIFICATION_LANGUAGE_POLICY") or os.path.expanduser(
    os.path.join("~", ".claude", "scripts", "notification_language.py"))


def _language_rule():
    from notification_receipts import ReceiptError, load_language_policy
    try:
        return load_language_policy(_RULE_PATH)
    except ReceiptError as exc:
        raise RouteError("the notification language rule is not installed at %s, so what arrives in "
                         "the channel cannot be checked for language" % _RULE_PATH) from exc

_API = "https://discord.com/api/v10"
_UA = "AgentCenter-RouteVerify/1.0 (+https://discord.com)"

# One row per notification TYPE, and the row is what makes "one channel each" checkable rather than
# merely intended. Adding a specific notification means adding a row here and provisioning it with
# agent_center_admin.py ensure-notification.
ROUTES = (
    {
        "stream": "model-mapping",
        "channel": "model-mapping",
        "category": "specific-notifications",
        "sender_dir": os.path.join("~", ".claude", "scripts", "cc-model-refresh"),
        "sender_file": "apply.py",
        "entry": "apply-map",
        "desc": "cc proxy: which model each tier maps to",
    },
    {
        "stream": "gateway-model-mapping",
        "channel": "gateway-model-mapping",
        "category": "specific-notifications",
        "sender_dir": os.path.join("~", ".claude", "scripts", "codexg-model-refresh"),
        "sender_file": "refresh.py",
        "entry": "notify",
        "desc": "codexg gateway: which model the gateway is pinned to",
    },
)


class RouteError(RuntimeError):
    pass


def route(stream: str) -> dict:
    for row in ROUTES:
        if row["stream"] == stream:
            return row
    raise RouteError("no route named %r; known routes: %s"
                     % (stream, ", ".join(r["stream"] for r in ROUTES)))


def load_sender(sender_dir: str, sender_file: str):
    """Import the production sender from its install directory, under a private module name.

    By path rather than vendored: a copy of the sender living in this repo would keep passing this
    check forever while the installed one went on DMing.
    """
    path = os.path.join(sender_dir, sender_file)
    if not os.path.isfile(path):
        raise RouteError("no sender at %s" % path)
    if sender_dir not in sys.path:
        sys.path.insert(0, sender_dir)
    spec = importlib.util.spec_from_file_location("_agentcenter_sender_under_test", path)
    if spec is None or spec.loader is None:
        raise RouteError("cannot load %s as a module" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _get(path: str, token: str):
    req = urllib.request.Request(_API + path, method="GET",
                                 headers={"Authorization": "Bot %s" % token, "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RouteError("Discord HTTP %s for GET %s" % (exc.code, path)) from exc
    except Exception as exc:
        raise RouteError("Discord GET %s failed: %s" % (path, exc)) from exc


def _resolve_channel(row: dict):
    """(channel_id, bot_token) for the row's stream, after checking its name and its category."""
    reg = relay.load_registry()
    entry = (reg.get("streams") or {}).get(row["stream"])
    if not isinstance(entry, dict) or not entry.get("channel_id"):
        raise RouteError("stream %r is not bound to a channel in the registry" % row["stream"])
    if entry.get("webhook"):
        raise RouteError("stream %r still carries a webhook, which pins it to the old channel"
                         % row["stream"])
    token = relay.bot_token(reg)
    if not token:
        raise RouteError("registry has no reader.bot_token; cannot read the channel back")
    channel_id = str(entry["channel_id"])

    channel = _get("/channels/%s" % channel_id, token)
    if str(channel.get("name", "")).casefold() != row["channel"].casefold():
        raise RouteError("stream %r is bound to #%s, expected #%s"
                         % (row["stream"], channel.get("name"), row["channel"]))
    parent_id = str(channel.get("parent_id") or "")
    if not parent_id:
        raise RouteError("#%s sits at the guild root, not inside a category" % row["channel"])
    parent = _get("/channels/%s" % parent_id, token)
    if str(parent.get("name", "")).casefold() != row["category"].casefold():
        raise RouteError("#%s is filed under %r, expected %r"
                         % (row["channel"], parent.get("name"), row["category"]))
    return channel_id, token


def _drive(sender, row: dict, marker: str) -> None:
    """Make the sender emit one message carrying `marker`, through its real notifier."""
    if getattr(sender, "STREAM", None) != row["stream"]:
        raise RouteError("sender announces on stream %r, expected %r"
                         % (getattr(sender, "STREAM", None), row["stream"]))
    # This sentence lands in the channel like any other notification, so it is written in the
    # channel's language (简体中文, instruction of 2026-09-09). It used to be English, and it was the
    # last thing still putting English in #model-mapping and #gateway-model-mapping after the senders
    # themselves had been translated -- a check that violates the rule it is checking.
    #
    # The marker stays as it is: it is an identifier, generated to be matched exactly.
    reason = "Agent Center 路由核验 %s：没有写入任何配置，也没有切换任何模型" % marker

    # What this probe QUOTES rather than writes, handed back so the language assertion can subtract
    # it: the marker is a generated identifier and the stream tag is the channel's own name. Anything
    # left over after these come out is prose, and prose is what the rule judges.
    verbatim = [marker, "Agent Center", row["stream"], row["channel"]]

    if row["entry"] == "notify":
        # A notifier that cannot say whether it delivered is the failure this whole script exists
        # to catch, so anything other than True is a failure.
        if sender.notify("[%s] %s" % (row["stream"], reason),
                         run_id='route:' + marker, verbatim=verbatim) is not True:
            raise RouteError("the sender's notify() did not report a successful delivery")
        return verbatim

    if row["entry"] == "apply-map":
        current = dict(sender.current_map())
        # A tier name no real map has, so nobody scrolling the channel mistakes this for a remap.
        forced = dict(current, **{"route-verification": marker})
        written = []
        changed = sender.apply_map(
            forced, reason,
            writer=written.append,
            reloader=lambda: (True, "未执行：路由核验"),
            run_id='route:' + marker,
            verbatim=verbatim,
        )
        if not changed:
            raise RouteError("apply_map reported no change, so it never reached its notifier")
        if written != [forced]:
            raise RouteError("apply_map did not hand the new map to the writer as expected")
        # Tier names and model ids are the map's own values, printed verbatim by design.
        return verbatim + ["None"] + [str(k) for k in forced] + [str(v) for v in forced.values()]

    raise RouteError("unknown entry style %r for stream %r" % (row["entry"], row["stream"]))


def verify_one(row: dict, attempts: int = 4, pause: float = 1.5) -> dict:
    sender_dir = os.path.expanduser(row["sender_dir"])
    sender = load_sender(sender_dir, row["sender_file"])
    channel_id, token = _resolve_channel(row)

    marker = "route-check-%s" % uuid.uuid4().hex[:12]
    verbatim = _drive(sender, row, marker)
    rule = _language_rule()

    for attempt in range(attempts):
        messages = _get("/channels/%s/messages?limit=25" % channel_id, token)
        for msg in messages if isinstance(messages, list) else []:
            body = msg.get("content") or ""
            if marker in body:
                # Arriving is not the same as being readable. This route was proved to deliver on
                # 2026-09-09 while the message it delivered was English, and delivery is the only
                # thing the rest of this function can see. The language is asserted on the text
                # DISCORD RETURNED rather than on what was handed to the sender, because the gap
                # between those two is precisely what an end-to-end check is for.
                offences = rule.offences(body, verbatim)
                if offences:
                    raise RouteError(
                        "the message reached #%s but it does not read in Simplified Chinese: %s -- "
                        "in %r" % (row["channel"], "; ".join(offences), body[:300]))
                return {"ok": True, "stream": row["stream"], "channel": row["channel"],
                        "category": row["category"], "entry": row["entry"],
                        "sender": os.path.join(sender_dir, row["sender_file"]),
                        "marker": marker, "attempts": attempt + 1, "language": "简体中文"}
        if attempt + 1 < attempts:
            time.sleep(pause)
    raise RouteError("the sender ran but its message never appeared in #%s (marker %s)"
                     % (row["channel"], marker))


def check_routes_are_distinct() -> None:
    """One channel per notification type is the point; two rows sharing one is the bug."""
    for field in ("stream", "channel"):
        seen = [r[field] for r in ROUTES]
        dupes = sorted({v for v in seen if seen.count(v) > 1})
        if dupes:
            raise RouteError("two notification types share the same %s: %s" % (field, dupes))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="verify_model_mapping_route.py")
    ap.add_argument("--stream", default=None,
                    help="check one route in this process; default is every route, each in its own")
    args = ap.parse_args(argv)

    try:
        check_routes_are_distinct()
        if args.stream:
            print(json.dumps(verify_one(route(args.stream)), ensure_ascii=False, indent=2))
            return 0
    except RouteError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    failed = []
    for row in ROUTES:
        proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--stream", row["stream"]],
                              stdin=subprocess.DEVNULL)
        if proc.returncode:
            failed.append(row["stream"])
    print(json.dumps({"ok": not failed, "checked": [r["stream"] for r in ROUTES],
                      "failed": failed}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
