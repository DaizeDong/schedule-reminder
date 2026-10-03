"""Notification routing and delivery receipts.

Explicit SCHEDULE_RELAY_CMD takes precedence. Otherwise the configured relay
sends to SCHEDULE_RELAY_STREAM (default reminders). A missing relay script uses
the standalone Big Brother sender. Legacy boolean delivery never proves external
readiness; a downstream receipt is required for that capability.

The transport modules resolve their own credentials. This module does not log them.
"""
from __future__ import annotations

import os
import json
import shlex
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _default_relay_path():
    return os.environ.get("SCHEDULE_RELAY_PY", os.path.join(_HERE, "relay.py"))


def _default_stream():
    return os.environ.get("SCHEDULE_RELAY_STREAM", "reminders")


def _run(argv):
    # encoding="utf-8": text=True otherwise decodes the child's stdout with the locale codepage
    # (GBK on a zh-CN box); harmless here since only the return code is used, but keep it consistent.
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    return r.returncode == 0


def notify(text):
    """Deliver `text` via the configured channel. Returns True on success, False on failure."""
    try:
        cmd_env = os.environ.get("SCHEDULE_RELAY_CMD")
        if cmd_env:  # explicit override / test seam, always wins
            return _run(shlex.split(cmd_env, posix=(os.name != "nt")) + [text])

        relay_py = _default_relay_path()
        if os.path.isfile(relay_py):  # Agent Center channel (the 2026-07-01 decision)
            return _run([sys.executable, relay_py, "send",
                         "--stream", _default_stream(), "--text", text])

        # Standalone install without relay.py: deliver via the native Big Brother DM so a reminder
        # is never dropped. (Replaces the old shell-out to the legacy DM notifier script.)
        return _standalone(text)
    except Exception as e:  # delivery failures are signalled by return value, not exceptions
        sys.stderr.write("notify: %s\n" % e)
        return False


def _standalone(text):
    """Legacy fallback confirms delivery only; it produces no readiness receipt."""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import bigbrother
    return bool(bigbrother.send_dm(text))


def deliver(text):
    """Return a downstream receipt when available, retaining legacy bool delivery.

    A successful legacy process has no receipt and cannot establish readiness.
    """
    command = os.environ.get('SCHEDULE_RELAY_CMD')
    if not command:
        relay_path = _default_relay_path()
        if not os.path.isfile(relay_path):
            return _standalone(text)
        if os.path.abspath(relay_path) == os.path.join(_HERE, 'relay.py'):
            import relay
            return relay.deliver(_default_stream(), text)
        argv = [sys.executable, _default_relay_path(), 'send', '--stream', _default_stream(), '--text', text]
    else:
        argv = shlex.split(command, posix=(os.name != 'nt')) + [text]
    result = subprocess.run(argv,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    if result.returncode:
        return False
    try:
        receipt = json.loads(result.stdout.strip())
    except (ValueError, TypeError):
        return True
    return receipt if isinstance(receipt, dict) else False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python notify.py <text>\n")
        sys.exit(2)
    sys.exit(0 if notify(sys.argv[1]) else 1)
