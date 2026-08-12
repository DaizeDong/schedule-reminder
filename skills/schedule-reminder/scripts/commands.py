#!/usr/bin/env python3
"""schedule-reminder — Agent Center COMMAND HANDLERS: the deterministic half of the inbound bus.

WHY THIS LAYER EXISTS
    Some inbound messages are commands, not conversation. "gradient og" asks for a picture; there
    is no ambiguity in it worth a model call, and a parser cannot be talked into running something
    else by a message from a channel. Before this layer, the only way to serve such a command was
    to write a whole second reader: its own guild sweep, its own cursor, its own Discord client, on
    its own timer. Two readers then disagreed about which channels exist, and a message that fell
    between them was consumed by the one that could not understand it and seen by nobody else.

    So a command is now a REGISTRATION, not a service. The bus does the reading; a handler declares
    what it answers to and gets handed the message.

CONTRACT WITH A HANDLER (deliberately tiny, and language agnostic)
    The bus runs `exec` as a subprocess and writes ONE json object, UTF-8, to its stdin:
        {"text":..., "channel_id":..., "stream":..., "message_id":..., "timestamp":...}
    Exit code 0 means the handler answered in the channel itself. Any other code, a crash or a
    timeout means it did not, and the bus says so in the channel rather than leaving silence.
    Nothing is passed on argv: Windows PowerShell mangles non-ASCII argv on its way to python, and
    a command typed in Chinese would arrive as mojibake.

REGISTRY (registry.commands, in the Agent Center config dir)
    "gradient": {
        "trigger": "^\\s*(?:gradient|bg|背景)\\b",   # python regex, matched per message
        "exec": ["python", "~/path/to/gradient_bot.py", "handle"],
        "timeout": 300,                               # optional, seconds
        "desc": "post a backdrop"                     # optional, for `list`
    }
    A channel opts out of handlers with streams.<name>.listen = false, which is how the reference
    channel full of example commands avoids having the bot answer its own documentation.

ROUTING ORDER, and why it is this way
    Handlers run FIRST, per message, and a claimed message never reaches the judgment chain. A
    command is cheaper, faster and more predictable resolved by a parser, and letting a model see
    "gradient x3" invites it to invent an action for it. What no handler claims flows on to
    dispatch exactly as before.

Stdlib only (+ the sibling relay module for reporting a failure back to the channel).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import relay  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_TIMEOUT = 300


def load(reg):
    """Compiled handler definitions from the registry. A malformed one is SKIPPED WITH A WARNING,
    never silently: a handler that vanishes because of a typo in its regex looks exactly like a bus
    that stopped reading, and that is the failure mode this whole layer exists to remove."""
    out = []
    for name, cfg in (reg.get("commands") or {}).items():
        if name.startswith("_"):
            continue          # registry convention: a leading underscore is a comment, not an entry
        if not isinstance(cfg, dict) or not cfg.get("trigger") or not cfg.get("exec"):
            sys.stderr.write("commands: %r is missing trigger or exec; skipped\n" % name)
            continue
        try:
            trigger = re.compile(cfg["trigger"], re.I)
        except re.error as e:
            sys.stderr.write("commands: %r has an invalid trigger (%s); skipped\n" % (name, e))
            continue
        argv = [os.path.expanduser(os.path.expandvars(str(a))) for a in cfg["exec"]]
        # A bare "python" is resolved against the CURRENT process's interpreter, not against PATH.
        # This runs from a scheduled task, and a task's PATH on Windows routinely contains only the
        # WindowsApps execution alias: a stub that `command -v` finds and that then runs nothing at
        # all. A handler that silently never executes is indistinguishable from a bus that stopped
        # reading, which is the failure this layer exists to make impossible.
        if argv and os.path.basename(argv[0]).lower() in ("python", "python.exe",
                                                          "python3", "python3.exe"):
            argv[0] = sys.executable or argv[0]
        out.append({"name": name, "trigger": trigger, "exec": argv,
                    "timeout": int(cfg.get("timeout") or DEFAULT_TIMEOUT),
                    "desc": cfg.get("desc") or ""})
    return out


def listens(reg, channel_id):
    """Whether handlers may run in this channel. Registered streams may opt out with listen:false;
    a channel nobody registered defaults to yes, because answering where the user typed is the
    entire point."""
    for s in (reg.get("streams") or {}).values():
        if str(s.get("channel_id") or "") == str(channel_id):
            return bool(s.get("listen", True))
    return True


def match(text, cmds):
    """The first handler whose trigger matches, or None. Order follows the registry."""
    t = text or ""
    for c in cmds:
        if c["trigger"].search(t):
            return c
    return None


def run(cmd, payload, log=None):
    """Execute one handler. Returns (ok, detail). Never raises: one bad handler must not take the
    tick down with it, because everything else in the batch still needs to be delivered."""
    blob = json.dumps(payload, ensure_ascii=False)
    # The payload is UTF-8 and routinely contains Chinese. A child process on Windows decodes its
    # stdin with the console code page unless told otherwise, so a command typed in Chinese reaches
    # the handler as mojibake or kills it outright. Pin the encoding for the child rather than
    # asking every handler to remember.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(cmd["exec"], input=blob, capture_output=True, text=True,
                           encoding="utf-8", timeout=cmd["timeout"], env=env)
    except subprocess.TimeoutExpired:
        return False, "timed out after %ds" % cmd["timeout"]
    except Exception as e:
        return False, "could not start: %s" % type(e).__name__
    if p.returncode != 0:
        detail = ((p.stderr or p.stdout or "").strip().splitlines() or ["exit %d" % p.returncode])[-1]
        return False, detail[:300]
    if log:
        log("commands: %s handled a message (exit 0)" % cmd["name"])
    return True, (p.stdout or "").strip()[:200]


def route(msgs, stream, channel_id, reg, log=None, post=True):
    """Split a batch: (claimed, remaining, results).

    `claimed` were answered (or attempted) by a handler and MUST NOT go on to the judgment chain.
    `remaining` are ordinary messages the chain should judge. A handler that fails still claims its
    message, and the failure is reported in the channel: re-routing a broken `gradient x3` into a
    model that will file it as a to-do is not a recovery, it is a second wrong answer."""
    results = []
    if not listens(reg, channel_id):
        return [], list(msgs), results
    cmds = load(reg)
    if not cmds:
        return [], list(msgs), results
    claimed, remaining = [], []
    for m in msgs:
        text = m.get("content") or ""
        c = match(text, cmds)
        if not c:
            remaining.append(m)
            continue
        ok, detail = run(c, {"text": text, "channel_id": str(channel_id), "stream": stream,
                             "message_id": m.get("id"), "timestamp": m.get("timestamp")}, log=log)
        claimed.append(m)
        results.append({"command": c["name"], "message_id": m.get("id"), "ok": ok, "detail": detail})
        if not ok:
            if log:
                log("commands: %s FAILED on %s: %s" % (c["name"], m.get("id"), detail))
            if post:
                relay.send("⚠️ 命令 `%s` 没跑成:%s" % (c["name"], detail), channel_id=str(channel_id))
    return claimed, remaining, results
