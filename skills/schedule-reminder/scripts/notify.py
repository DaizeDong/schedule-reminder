"""Notification routing and delivery receipts.

Delivery always goes through the sibling relay module in this process, to
SCHEDULE_RELAY_STREAM (default reminders). relay itself falls back to the Big
Brother DM when the stream or registry is missing. SCHEDULE_RELAY_CMD is the one
override: a command that receives the text as its last argument. Tests use it so
no tick reaches real Discord.

The transport modules resolve their own credentials. This module does not log them.
"""
from __future__ import annotations

import os
import json
import shlex
import subprocess
import sys


def _default_stream():
    return os.environ.get("SCHEDULE_RELAY_STREAM", "reminders")


def _override():
    """Return the SCHEDULE_RELAY_CMD argv, or None when delivery stays in process."""
    command = os.environ.get("SCHEDULE_RELAY_CMD")
    return shlex.split(command, posix=(os.name != "nt")) if command else None


def _run(argv):
    # encoding="utf-8": text=True otherwise decodes the child's stdout with the locale codepage.
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=30,
                          **({"creationflags": 0x08000000} if os.name == "nt" else {}))


def notify_event(event, text, **delivery_options):
    """Return the durable business-event receipt for an explicitly selected owner event."""
    from notification_receipts import deliver
    return deliver(event, text, **delivery_options)


def notify_occurrence(run_id, text, *, phase='reminder', condition='due',
                      db_path=None, retry_failed=False):
    """Durable occurrence delivery: in-process relay unless SCHEDULE_RELAY_CMD overrides it."""
    import notification_client as client
    stream = _default_stream()
    command = _override()
    options = ({'command': client.command_policy(command)} if command
               else {'fallback': 'big_brother'})
    return client.submit('schedule-reminder', run_id, phase, condition, stream, text,
                         language='preserve', db_path=db_path, retry_failed=retry_failed, **options)


def notify(text, *, run_id=None, phase='reminder', condition='due', retry_failed=False):
    """Return boolean delivery, using durable receipts when an occurrence ID is supplied."""
    try:
        if run_id is not None:
            import notification_client as client
            receipt = notify_occurrence(run_id, text, phase=phase, condition=condition,
                                        retry_failed=retry_failed)
            if receipt['state'] != 'sent':
                sys.stderr.write('notify: ' + client.detail(receipt) + '\n')
            return receipt['state'] == 'sent'
        if retry_failed:
            sys.stderr.write('notify: retry requires a stable occurrence ID\n')
            return False
        command = _override()
        if command:
            return _run(command + [text]).returncode == 0
        import relay
        return bool(relay.relay(_default_stream(), text))
    except Exception as e:  # delivery failures are signalled by return value, not exceptions
        sys.stderr.write("notify: %s\n" % e)
        return False


def deliver(text):
    """Deliver one reminder and return relay's receipt dict (or a bool from relay's fallback)."""
    command = _override()
    if not command:
        import relay
        return relay.deliver(_default_stream(), text)
    result = _run(command + [text])
    if result.returncode:
        return False
    try:
        receipt = json.loads(result.stdout.strip())
    except (ValueError, TypeError):
        return True
    return receipt if isinstance(receipt, dict) else False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Deliver a reminder or stable owner notification')
    parser.add_argument('text')
    parser.add_argument('--run-id')
    parser.add_argument('--phase', default='reminder')
    parser.add_argument('--condition', default='due')
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    sys.exit(0 if notify(args.text, run_id=args.run_id, phase=args.phase,
                         condition=args.condition, retry_failed=args.retry_failed) else 1)
