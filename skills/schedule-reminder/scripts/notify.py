#!/usr/bin/env python3
"""schedule-reminder — pluggable notification channel.

Default channel = the **Agent Center `#reminders` channel**, via this repo's own `relay.py`
(`relay.py send --stream reminders`). That is the standing decision (2026-07-01): every skill
notifies into its own Agent Center channel; the Big Brother DM is no longer a notification target.

  NOTE for whoever owns the Discord: a channel post does NOT push to your phone unless that
  channel's notifications are set to All Messages. A DM always pushes. Routing reminders to a
  channel is only safe if #reminders is actually configured to notify you.

Resolution order (first one that exists wins):
  1. SCHEDULE_RELAY_CMD  — explicit override; text appended as final argv. Also the **test seam**
     (tests point it at a stub, so no real Discord push happens).
  2. relay.py            — `send --stream <SCHEDULE_RELAY_STREAM|reminders>` (the Agent Center
     egress; relay.py itself falls back to the DM if that stream is unconfigured, so a reminder is
     never silently lost).
  3. bigbrother DM       — the native Big Brother DM sender (`bigbrother.send_dm`), only if relay.py
     is missing (standalone install). Replaces the old shell-out to `discord_relay/send.py`.

Contract: notify(text) -> bool  (True = delivered, False = failed; never raises for delivery errors).

Env:
  SCHEDULE_RELAY_CMD     full command to run; reminder text appended as last arg (overrides all)
  SCHEDULE_RELAY_PY      path to relay.py       (default: alongside this file)
  SCHEDULE_RELAY_STREAM  Agent Center stream     (default: "reminders")

Secrets: the relay/bigbrother read their token/webhook from the registry; this module never reads,
logs, or echoes any of them.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _default_relay_path():
    return os.environ.get("SCHEDULE_RELAY_PY", os.path.join(_HERE, "relay.py"))


def _default_stream():
    return os.environ.get("SCHEDULE_RELAY_STREAM", "reminders")


def _run(argv):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def notify(text):
    """Deliver `text` via the configured channel. Returns True on success, False on failure."""
    try:
        cmd_env = os.environ.get("SCHEDULE_RELAY_CMD")
        if cmd_env:  # explicit override / test seam — always wins
            return _run(shlex.split(cmd_env, posix=(os.name != "nt")) + [text])

        relay_py = _default_relay_path()
        if os.path.isfile(relay_py):  # Agent Center channel (the 2026-07-01 decision)
            return _run([sys.executable, relay_py, "send",
                         "--stream", _default_stream(), "--text", text])

        # Standalone install without relay.py: deliver via the native Big Brother DM so a reminder
        # is never dropped. (Replaces the old shell-out to the legacy DM notifier script.)
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        import bigbrother  # noqa: E402  (local sibling module)
        return bool(bigbrother.send_dm(text))
    except Exception as e:  # delivery failures are signalled by return value, not exceptions
        sys.stderr.write("notify: %s\n" % e)
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python notify.py <text>\n")
        sys.exit(2)
    sys.exit(0 if notify(sys.argv[1]) else 1)
