#!/usr/bin/env python3
"""schedule-reminder — pluggable notification channel.

Default channel = the local Discord relay (the legacy DM notifier script). Swappable without
touching business logic: set SCHEDULE_RELAY_CMD to any command and the reminder text is appended
as the final argv. This is also the test seam — tests point SCHEDULE_RELAY_CMD at a stub.

Contract: notify(text) -> bool  (True = delivered, False = failed; never raises for delivery errors).

Env:
  SCHEDULE_RELAY_CMD   full command to run; reminder text appended as last arg (overrides default)
  SCHEDULE_RELAY_SEND  path to discord_relay/send.py (default the legacy DM notifier script)

Secrets: the relay reads its own bot token from its own config; this module never reads, logs, or
echoes any token.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys


def _default_send_path():
    return os.environ.get(
        "SCHEDULE_RELAY_SEND",
        os.path.join(os.path.expanduser("~"), ".claude", "discord_relay", "send.py"),
    )


def notify(text):
    """Deliver `text` via the configured channel. Returns True on success, False on failure."""
    cmd_env = os.environ.get("SCHEDULE_RELAY_CMD")
    try:
        if cmd_env:
            argv = shlex.split(cmd_env, posix=(os.name != "nt")) + [text]
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            return r.returncode == 0
        send_py = _default_send_path()
        if not os.path.isfile(send_py):
            sys.stderr.write("notify: relay not found at %s\n" % send_py)
            return False
        r = subprocess.run([sys.executable, send_py, text],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception as e:  # delivery failures are signalled by return value, not exceptions
        sys.stderr.write("notify: %s\n" % e)
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python notify.py <text>\n")
        sys.exit(2)
    sys.exit(0 if notify(sys.argv[1]) else 1)
